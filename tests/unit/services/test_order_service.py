from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.common.exceptions import (
    BusinessRuleError,
    DataAccessError,
    ResourceConflictError,
    ResourceNotFoundError,
    ServiceUnavailableError,
)
from app.enums.order_enums import OrderDirection
from app.models.account import Account
from app.models.order import Order
from app.repositories.account_repository import AccountRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.outbox_repository import OutboxRepository
from app.schemas.order_schema import OrderCreateRequest
from app.services.fee_calculator import FeeCalculator
from app.services.account_access_scope import AccountAccessScope
from app.services.margin_calculator import MarginCalculator
from app.services.order_freeze_service import OrderFreezeService
from app.services.order_service import OrderService
from app.services.order_price_resolver import ResolvedOrderPrice
from app.services.order_validation_service import OrderValidationService


TRADING_DAY = date(2026, 7, 17)


def make_request(direction=OrderDirection.BUY, **overrides):
    values = {
        "client_order_id": "CLIENT-000001",
        "account_id": "A001",
        "exchange_id": "SHFE",
        "symbol": "RB2610",
        "direction": direction,
        "offset_flag": "OPEN",
        "order_type": "LIMIT",
        "limit_price": Decimal("3500"),
        "volume": 2,
    }
    values.update(overrides)
    return OrderCreateRequest(**values)


def make_rules(**overrides):
    instrument = SimpleNamespace(
        order_book_id="RB2610",
        symbol="RB2610",
        exchange_id="SHFE",
        instrument_type="FUTURES",
        is_active=True,
        min_volume=1,
        max_volume=100,
        price_tick=Decimal("1"),
        contract_multiplier=Decimal("10"),
    )
    margin_rule = SimpleNamespace(
        long_margin_rate=Decimal("0.12"),
        short_margin_rate=Decimal("0.13"),
    )
    fee_rule = SimpleNamespace(
        commission_type="BY_VOLUME",
        open_commission=Decimal("3"),
        close_commission=Decimal("3"),
        close_today_commission=Decimal("6"),
    )
    values = {
        "instrument": instrument,
        "margin_rule": margin_rule,
        "fee_rule": fee_rule,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_account(**overrides):
    values = {
        "status": "NORMAL",
        "trading_day": TRADING_DAY,
        "available_cash": Decimal("100000"),
        "frozen_margin": Decimal("0"),
        "frozen_commission": Decimal("0"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_existing_order(**overrides):
    values = {
        "order_id": "EXISTING",
        "account_id": "A001",
        "exchange_id": "SHFE",
        "symbol": "RB2610",
        "direction": "BUY",
        "offset_flag": "OPEN",
        "order_type": "LIMIT",
        "limit_price": Decimal("3500.000000"),
        "total_volume": 2,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_service(
    *,
    order_repository=None,
    account_repository=None,
    rule_query_service=None,
    outbox_repository=None,
    position_repository=None,
    allocation_repository=None,
    close_allocator=None,
    order_price_resolver=None,
):
    order_repository = order_repository or Mock()
    account_repository = account_repository or Mock()
    rule_query_service = rule_query_service or Mock()
    outbox_repository = outbox_repository or Mock()
    return OrderService(
        order_repository=order_repository,
        account_repository=account_repository,
        rule_query_service=rule_query_service,
        validation_service=OrderValidationService(),
        freeze_service=OrderFreezeService(),
        margin_calculator=MarginCalculator(),
        fee_calculator=FeeCalculator(),
        outbox_repository=outbox_repository,
        position_repository=position_repository,
        allocation_repository=allocation_repository,
        close_allocator=close_allocator,
        trading_day_provider=lambda: TRADING_DAY,
        order_id_factory=lambda: "O20260717000001",
        event_id_factory=lambda: "EVT-000001",
        default_access_scope=AccountAccessScope.admin(),
        order_price_resolver=order_price_resolver,
    )


@pytest.mark.parametrize(
    ("direction", "expected_margin"),
    [
        (OrderDirection.BUY, Decimal("8400.000000")),
        (OrderDirection.SELL, Decimal("9100.000000")),
    ],
)
def test_create_open_order_success(direction, expected_margin):
    db = Mock()
    order_repository = Mock()
    account_repository = Mock()
    rule_query_service = Mock()
    outbox_repository = Mock()
    account = make_account()
    created_order = SimpleNamespace(order_id="O20260717000001")
    order_repository.get_by_client_order_id.side_effect = [None, None]
    order_repository.create.return_value = created_order
    account_repository.get_by_account_id_for_update.return_value = account
    rule_query_service.get_order_rules.return_value = make_rules()
    service = make_service(
        order_repository=order_repository,
        account_repository=account_repository,
        rule_query_service=rule_query_service,
        outbox_repository=outbox_repository,
    )

    result = service.create_order(db=db, request=make_request(direction))

    assert result is created_order
    assert account.available_cash == (
        Decimal("100000") - expected_margin - Decimal("6")
    )
    assert account.frozen_margin == expected_margin
    assert account.frozen_commission == Decimal("6.000000")
    assert order_repository.create.call_args.kwargs["status"] == "ACCEPTED"
    assert order_repository.create.call_args.kwargs["total_volume"] == 2
    event_args = next(
        call.kwargs
        for call in outbox_repository.create_event.call_args_list
        if call.kwargs["event_type"] == "ORDER_ACCEPTED"
    )
    assert event_args["aggregate_type"] == "ORDER"
    assert event_args["aggregate_id"] == "O20260717000001"
    assert event_args["event_type"] == "ORDER_ACCEPTED"
    assert event_args["payload"]["limit_price"] == "3500.000000"
    assert event_args["payload"]["direction"] == direction.value
    db.commit.assert_called_once_with()
    db.refresh.assert_called_once_with(created_order)


def test_market_order_freezes_with_persisted_protection_price():
    db = Mock()
    order_repository = Mock()
    account_repository = Mock()
    rule_query_service = Mock()
    outbox_repository = Mock()
    resolver = Mock()
    resolver.resolve.return_value = ResolvedOrderPrice(
        resolved_price=Decimal("3600"),
        market_protection_price=Decimal("3600"),
        snapshot_time=None,
        snapshot_source="YMM_LIVE_DATA",
        snapshot_event_id="TICK-1",
        snapshot_stream_message_id="1-0",
        bid1=Decimal("3499"),
        bid_volume1=3,
        ask1=Decimal("3500"),
        ask_volume1=1,
        last_price=Decimal("3498"),
    )
    account = make_account()
    created_order = SimpleNamespace(order_id="O20260717000001")
    order_repository.get_by_client_order_id.side_effect = [None, None]
    order_repository.create.return_value = created_order
    account_repository.get_by_account_id.return_value = account
    account_repository.get_by_account_id_for_update.return_value = account
    rule_query_service.get_order_rules.return_value = make_rules()
    service = make_service(
        order_repository=order_repository,
        account_repository=account_repository,
        rule_query_service=rule_query_service,
        outbox_repository=outbox_repository,
        order_price_resolver=resolver,
    )

    service.create_order(
        db=db,
        request=make_request(order_type="MARKET", limit_price=None),
    )

    created = order_repository.create.call_args.kwargs
    assert created["limit_price"] == Decimal("3600")
    assert created["market_protection_price"] == Decimal("3600")
    assert created["frozen_margin"] == Decimal("8640.000000")
    assert account.frozen_margin == Decimal("8640.000000")
    event = next(
        call.kwargs["payload"]
        for call in outbox_repository.create_event.call_args_list
        if call.kwargs["event_type"] == "ORDER_ACCEPTED"
    )
    assert event["order_type"] == "MARKET"
    assert event["resolved_price"] == "3600.000000"


def test_missing_market_price_bootstraps_snapshot_then_accepts_once():
    db = Mock()
    order_repository = Mock()
    account_repository = Mock()
    rule_query_service = Mock()
    outbox_repository = Mock()
    resolver = Mock()
    resolver.resolve.side_effect = [
        ServiceUnavailableError(
            "无实时行情",
            error_code="ORDER_PRICE_MARKET_DATA_UNAVAILABLE",
        ),
        ResolvedOrderPrice(
            resolved_price=Decimal("3600"),
            market_protection_price=Decimal("3600"),
            snapshot_time=None,
            snapshot_source="YMM_DATA_SDK",
            snapshot_event_id="SNAPSHOT-1",
            snapshot_stream_message_id="2-0",
            bid1=Decimal("3599"),
            bid_volume1=3,
            ask1=Decimal("3600"),
            ask_volume1=2,
            last_price=Decimal("3598"),
        ),
    ]
    account = make_account()
    created_order = SimpleNamespace(order_id="O20260717000001")
    order_repository.get_by_client_order_id.side_effect = [None, None]
    order_repository.create.return_value = created_order
    account_repository.get_by_account_id.return_value = account
    account_repository.get_by_account_id_for_update.return_value = account
    rules = make_rules()
    rule_query_service.get_order_rules.return_value = rules
    service = make_service(
        order_repository=order_repository,
        account_repository=account_repository,
        rule_query_service=rule_query_service,
        outbox_repository=outbox_repository,
        order_price_resolver=resolver,
    )
    mapping = Mock()
    mapping.to_source.side_effect = lambda code: f"SRC-{code}"
    mapping.to_internal.side_effect = lambda code: code.removeprefix("SRC-")
    mapping_service = Mock()
    mapping_service.build_snapshot.return_value = mapping
    snapshot_client = Mock()
    snapshot_client.fetch_latest_many.return_value = {
        "SRC-RB2610": {"order_book_id": "SRC-RB2610"}
    }
    market_data_service = Mock()
    service.market_pre_subscription_store = Mock()
    service.database_snapshot_client = snapshot_client
    service.market_data_service = market_data_service
    service.market_data_code_mapping_service = mapping_service

    result = service.create_order(
        db=db,
        request=make_request(order_type="MARKET", limit_price=None),
    )

    assert result is created_order
    service.market_pre_subscription_store.request_codes.assert_called_once_with(
        account_id="A001",
        codes={"RB2610"},
    )
    market_data_service.process.assert_called_once()
    assert market_data_service.process.call_args.kwargs["data"]["order_book_id"] == "RB2610"
    assert resolver.resolve.call_count == 2
    assert resolver.resolve.call_args_list[1].kwargs["allow_bootstrap_snapshot"] is True
    assert order_repository.create.call_args.kwargs["status"] == "ACCEPTED"


@pytest.mark.parametrize(
    "risk_state",
    ["MARGIN_DEFICIT", "LIQUIDATION_PENDING", "LIQUIDATING", "VALUATION_UNAVAILABLE"],
)
def test_all_open_products_use_unified_account_risk_block(risk_state):
    db = Mock()
    order_repository = Mock()
    account_repository = Mock()
    rule_query_service = Mock()
    account = make_account(risk_state=risk_state)
    order_repository.get_by_client_order_id.side_effect = [None, None]
    account_repository.get_by_account_id.return_value = account
    account_repository.get_by_account_id_for_update.return_value = account
    rule_query_service.get_order_rules.return_value = make_rules()
    service = make_service(
        order_repository=order_repository,
        account_repository=account_repository,
        rule_query_service=rule_query_service,
    )

    with pytest.raises(BusinessRuleError) as exc_info:
        service.create_order(db=db, request=make_request())

    assert exc_info.value.error_code == "ACCOUNT_RISK_OPEN_BLOCKED"
    order_repository.create.assert_not_called()
    db.rollback.assert_called_once()


def test_duplicate_client_order_id_returns_existing_without_freeze():
    db = Mock()
    existing = make_existing_order()
    order_repository = Mock()
    order_repository.get_by_client_order_id.return_value = existing
    account_repository = Mock()
    account_repository.get_by_account_id_for_update.return_value = (
        make_account()
    )
    rule_query_service = Mock()
    service = make_service(
        order_repository=order_repository,
        account_repository=account_repository,
        rule_query_service=rule_query_service,
    )

    result = service.create_order(db=db, request=make_request())

    assert result is existing
    account_repository.get_by_account_id_for_update.assert_not_called()
    rule_query_service.get_order_rules.assert_not_called()
    order_repository.create.assert_not_called()
    db.expunge.assert_called_once_with(existing)
    db.commit.assert_called_once()
    db.refresh.assert_not_called()


def test_idempotent_market_retry_does_not_reread_snapshot_or_refreeze():
    db = Mock()
    existing = make_existing_order(
        order_type="MARKET",
        submitted_limit_price=None,
        resolved_price=Decimal("3600"),
    )
    order_repository = Mock()
    order_repository.get_by_client_order_id.return_value = existing
    account_repository = Mock()
    account_repository.get_by_account_id.return_value = make_account()
    resolver = Mock()
    service = make_service(
        order_repository=order_repository,
        account_repository=account_repository,
        order_price_resolver=resolver,
    )

    result = service.create_order(
        db=db,
        request=make_request(order_type="MARKET", limit_price=None),
    )

    assert result is existing
    resolver.resolve.assert_not_called()
    order_repository.create.assert_not_called()


def test_missing_access_scope_never_falls_back_to_admin():
    service = make_service()
    service.default_access_scope = None

    with pytest.raises(ValueError, match="授权范围"):
        service.create_order(Mock(), make_request())


@pytest.mark.parametrize(
    ("direction", "offset_flag", "position_direction", "commission"),
    [
        ("SELL", "CLOSE_TODAY", "LONG", Decimal("12.000000")),
        ("BUY", "CLOSE_TODAY", "SHORT", Decimal("12.000000")),
        ("SELL", "CLOSE_YESTERDAY", "LONG", Decimal("6.000000")),
        ("BUY", "CLOSE", "SHORT", Decimal("12.000000")),
    ],
)
def test_create_close_order_freezes_commission_and_position(
    direction,
    offset_flag,
    position_direction,
    commission,
):
    db = Mock()
    order_repository = Mock()
    order_repository.get_by_client_order_id.side_effect = [None, None]
    created_order = SimpleNamespace(order_id="O20260717000001")
    order_repository.create.return_value = created_order
    account = make_account()
    account_repository = Mock()
    account_repository.get_by_account_id_for_update.return_value = account
    rule_query_service = Mock()
    rule_query_service.get_order_rules.return_value = make_rules()
    detail = SimpleNamespace(
        id=1,
        position_detail_id="PD-1",
        open_trading_day=(
            TRADING_DAY - timedelta(days=1)
            if offset_flag == "CLOSE_YESTERDAY"
            else TRADING_DAY
        ),
        remaining_volume=5,
        frozen_volume=0,
        updated_at=None,
    )
    position = SimpleNamespace(
        position_id="P-1",
        instrument_type="FUTURES",
        total_volume=5,
        frozen_volume=0,
        settlement_locked_volume=0,
        available_volume=5,
        updated_at=None,
    )
    position_repository = Mock()
    position_repository.get_for_update.return_value = position
    position_repository.list_available_details_for_update.return_value = [
        detail
    ]
    allocation_repository = Mock()
    outbox_repository = Mock()
    service = make_service(
        order_repository=order_repository,
        account_repository=account_repository,
        rule_query_service=rule_query_service,
        outbox_repository=outbox_repository,
        position_repository=position_repository,
        allocation_repository=allocation_repository,
    )

    result = service.create_order(
        db,
        make_request(
            direction=direction,
            offset_flag=offset_flag,
        ),
    )

    assert result is created_order
    assert account.available_cash == Decimal("100000") - commission
    assert account.frozen_margin == Decimal("0")
    assert account.frozen_commission == commission
    assert position.frozen_volume == 2
    assert position.available_volume == 3
    assert detail.frozen_volume == 2
    assert (
        position_repository.get_for_update.call_args.kwargs["direction"]
        == position_direction
    )
    create_args = order_repository.create.call_args.kwargs
    assert create_args["frozen_margin"] == Decimal("0.000000")
    assert create_args["frozen_commission"] == commission
    assert create_args["frozen_position_volume"] == 2
    allocation = allocation_repository.add.call_args.args[1]
    assert allocation.original_frozen_volume == 2
    assert allocation.remaining_frozen_volume == 2
    assert allocation.status == "ACTIVE"
    assert (
        next(
            call.kwargs
            for call in outbox_repository.create_event.call_args_list
            if call.kwargs["event_type"] == "ORDER_ACCEPTED"
        )["payload"]["frozen_position_volume"]
        == 2
    )


def test_plain_close_splits_yesterday_and_today_frozen_commission():
    db = Mock()
    order_repository = Mock()
    order_repository.get_by_client_order_id.side_effect = [None, None]
    created_order = SimpleNamespace(order_id="O20260717000001")
    order_repository.create.return_value = created_order
    account = make_account()
    account_repository = Mock()
    account_repository.get_by_account_id_for_update.return_value = account
    rule_query_service = Mock()
    rule_query_service.get_order_rules.return_value = make_rules()
    yesterday = SimpleNamespace(
        id=1,
        position_detail_id="PD-Y",
        open_trading_day=TRADING_DAY - timedelta(days=1),
        remaining_volume=5,
        frozen_volume=0,
        updated_at=None,
    )
    today = SimpleNamespace(
        id=2,
        position_detail_id="PD-T",
        open_trading_day=TRADING_DAY,
        remaining_volume=5,
        frozen_volume=0,
        updated_at=None,
    )
    position = SimpleNamespace(
        position_id="P-1",
        instrument_type="FUTURES",
        total_volume=10,
        frozen_volume=0,
        settlement_locked_volume=0,
        available_volume=10,
        updated_at=None,
    )
    position_repository = Mock()
    position_repository.get_for_update.return_value = position
    position_repository.list_available_details_for_update.return_value = [
        yesterday,
        today,
    ]
    allocation_repository = Mock()
    service = make_service(
        order_repository=order_repository,
        account_repository=account_repository,
        rule_query_service=rule_query_service,
        outbox_repository=Mock(),
        position_repository=position_repository,
        allocation_repository=allocation_repository,
    )

    service.create_order(
        db,
        make_request(
            direction="SELL",
            offset_flag="CLOSE",
            volume=10,
        ),
    )

    assert account.frozen_commission == Decimal("45.000000")
    assert account.available_cash == Decimal("99955.000000")
    assert order_repository.create.call_args.kwargs[
        "frozen_commission"
    ] == Decimal("45.000000")
    allocations = [
        item.args[1] for item in allocation_repository.add.call_args_list
    ]
    assert [
        (
            item.position_detail_id,
            item.resolved_offset_flag,
            item.commission_parameter,
            item.remaining_frozen_commission,
        )
        for item in allocations
    ] == [
        ("PD-Y", "CLOSE_YESTERDAY", Decimal("3"), Decimal("15.000000")),
        ("PD-T", "CLOSE_TODAY", Decimal("6"), Decimal("30.000000")),
    ]


def test_close_order_freeze_uses_bucket_total_across_multiple_details():
    """同一平今费用桶拆成两条持仓明细时只计算、量化一次总手续费。"""

    db = Mock()
    order_repository = Mock()
    order_repository.get_by_client_order_id.side_effect = [None, None]
    order_repository.create.return_value = SimpleNamespace(
        order_id="O20260717000001"
    )
    account = make_account()
    account_repository = Mock()
    account_repository.get_by_account_id_for_update.return_value = account
    rules = make_rules()
    rules.fee_rule.commission_type = "BY_AMOUNT"
    rules.fee_rule.close_today_commission = Decimal("0.000001000015")
    rule_query_service = Mock()
    rule_query_service.get_order_rules.return_value = rules
    details = [
        SimpleNamespace(
            id=index,
            position_detail_id=f"PD-{index}",
            open_trading_day=TRADING_DAY,
            remaining_volume=1,
            frozen_volume=0,
            updated_at=None,
        )
        for index in (1, 2)
    ]
    position = SimpleNamespace(
        position_id="P-1",
        instrument_type="FUTURES",
        total_volume=2,
        frozen_volume=0,
        settlement_locked_volume=0,
        available_volume=2,
        updated_at=None,
    )
    position_repository = Mock()
    position_repository.get_for_update.return_value = position
    position_repository.list_available_details_for_update.return_value = (
        details
    )
    allocation_repository = Mock()
    service = make_service(
        order_repository=order_repository,
        account_repository=account_repository,
        rule_query_service=rule_query_service,
        outbox_repository=Mock(),
        position_repository=position_repository,
        allocation_repository=allocation_repository,
    )

    service.create_order(
        db,
        make_request(
            direction="SELL",
            offset_flag="CLOSE_TODAY",
            limit_price=Decimal("3520"),
            volume=2,
        ),
    )

    allocations = [
        item.args[1] for item in allocation_repository.add.call_args_list
    ]
    assert [item.original_frozen_commission for item in allocations] == [
        Decimal("0.035201"),
        Decimal("0.035200"),
    ]
    assert sum(
        item.original_frozen_commission for item in allocations
    ) == Decimal("0.070401")
    assert (
        order_repository.create.call_args.kwargs["frozen_commission"]
        == account.frozen_commission
        == Decimal("0.070401")
    )


def test_duplicate_detected_after_account_lock_does_not_freeze_twice():
    db = Mock()
    existing = make_existing_order()
    account = make_account()
    order_repository = Mock()
    order_repository.get_by_client_order_id.side_effect = [None, existing]
    account_repository = Mock()
    account_repository.get_by_account_id_for_update.return_value = account
    rule_query_service = Mock()
    rule_query_service.get_order_rules.return_value = make_rules()
    service = make_service(
        order_repository=order_repository,
        account_repository=account_repository,
        rule_query_service=rule_query_service,
    )

    result = service.create_order(db=db, request=make_request())

    assert result is existing
    assert account.available_cash == Decimal("100000")
    order_repository.create.assert_not_called()
    db.expunge.assert_called_once_with(existing)
    db.commit.assert_called_once()
    db.refresh.assert_not_called()


def test_normal_user_uses_scoped_idempotency_and_account_lock_queries():
    """普通用户的幂等读取和账户锁都必须在SQL中携带user_id。"""

    db = Mock()
    account = make_account()
    order_repository = Mock()
    order_repository.get_by_client_order_id_for_user.return_value = None
    order_repository.get_by_client_order_id.return_value = None
    order_repository.create.return_value = SimpleNamespace(
        order_id="O20260717000001"
    )
    account_repository = Mock()
    account_repository.get_owned_account.return_value = account
    account_repository.get_owned_account_for_update.return_value = account
    rule_query_service = Mock()
    rule_query_service.get_order_rules.return_value = make_rules()
    service = make_service(
        order_repository=order_repository,
        account_repository=account_repository,
        rule_query_service=rule_query_service,
        outbox_repository=Mock(),
    )

    service.create_order(
        db,
        make_request(),
        access_scope=AccountAccessScope.for_user("U001"),
    )

    order_repository.get_by_client_order_id_for_user.assert_called_once_with(
        db=db,
        account_id="A001",
        client_order_id="CLIENT-000001",
        user_id="U001",
    )
    account_repository.get_owned_account_for_update.assert_called_once_with(
        db=db,
        account_id="A001",
        user_id="U001",
    )
    account_repository.get_by_account_id_for_update.assert_not_called()


def test_unauthorized_user_never_uses_unscoped_account_lock():
    db = Mock()
    order_repository = Mock()
    order_repository.get_by_client_order_id_for_user.return_value = None
    account_repository = Mock()
    account_repository.get_owned_account.return_value = None
    rule_query_service = Mock()
    rule_query_service.get_order_rules.return_value = make_rules()
    service = make_service(
        order_repository=order_repository,
        account_repository=account_repository,
        rule_query_service=rule_query_service,
    )

    with pytest.raises(ResourceNotFoundError) as exc_info:
        service.create_order(
            db,
            make_request(),
            access_scope=AccountAccessScope.for_user("U-OTHER"),
        )

    assert exc_info.value.error_code == "RESOURCE_NOT_FOUND"
    account_repository.get_owned_account.assert_called_once()
    account_repository.get_owned_account_for_update.assert_not_called()
    account_repository.get_by_account_id_for_update.assert_not_called()
    rule_query_service.get_order_rules.assert_not_called()
    order_repository.get_by_client_order_id.assert_not_called()
    order_repository.create.assert_not_called()


def test_integrity_conflict_recovery_remains_user_scoped():
    db = Mock()
    existing = make_existing_order()
    order_repository = Mock()
    order_repository.get_by_client_order_id_for_user.side_effect = [
        None,
        existing,
    ]
    order_repository.get_by_client_order_id.return_value = None
    # 服务只对IntegrityError执行幂等恢复，使用其真实异常构造冲突。
    order_repository.create.side_effect = IntegrityError(
        "insert order",
        {},
        Exception("unique"),
    )
    account_repository = Mock()
    account_repository.get_owned_account_for_update.return_value = (
        make_account()
    )
    rule_query_service = Mock()
    rule_query_service.get_order_rules.return_value = make_rules()
    service = make_service(
        order_repository=order_repository,
        account_repository=account_repository,
        rule_query_service=rule_query_service,
    )

    result = service.create_order(
        db,
        make_request(),
        access_scope=AccountAccessScope.for_user("U001"),
    )

    assert result is existing
    assert (
        order_repository.get_by_client_order_id_for_user.call_count == 2
    )
    assert all(
        call.kwargs["user_id"] == "U001"
        for call in (
            order_repository.get_by_client_order_id_for_user.call_args_list
        )
    )
    db.rollback.assert_called_once()
    db.commit.assert_called_once()


def test_first_idempotency_hit_rejects_changed_request_before_rules_or_lock():
    db = Mock()
    order_repository = Mock()
    order_repository.get_by_client_order_id.return_value = (
        make_existing_order(direction="SELL")
    )
    account_repository = Mock()
    rule_query_service = Mock()
    outbox_repository = Mock()
    service = make_service(
        order_repository=order_repository,
        account_repository=account_repository,
        rule_query_service=rule_query_service,
        outbox_repository=outbox_repository,
    )

    with pytest.raises(ResourceConflictError) as exc_info:
        service.create_order(db, make_request())

    assert exc_info.value.error_code == "IDEMPOTENCY_KEY_REUSED"
    rule_query_service.get_order_rules.assert_not_called()
    account_repository.get_by_account_id_for_update.assert_not_called()
    order_repository.create.assert_not_called()
    outbox_repository.create_event.assert_not_called()
    db.rollback.assert_called_once()


def test_second_idempotency_hit_rejects_changed_request_before_freeze():
    db = Mock()
    order_repository = Mock()
    order_repository.get_by_client_order_id.side_effect = [
        None,
        make_existing_order(limit_price=Decimal("3501")),
    ]
    account = make_account()
    account_repository = Mock()
    account_repository.get_by_account_id_for_update.return_value = account
    rule_query_service = Mock()
    rule_query_service.get_order_rules.return_value = make_rules()
    outbox_repository = Mock()
    service = make_service(
        order_repository=order_repository,
        account_repository=account_repository,
        rule_query_service=rule_query_service,
        outbox_repository=outbox_repository,
    )

    with pytest.raises(ResourceConflictError) as exc_info:
        service.create_order(db, make_request())

    assert exc_info.value.error_code == "IDEMPOTENCY_KEY_REUSED"
    assert account.available_cash == Decimal("100000")
    order_repository.create.assert_not_called()
    outbox_repository.create_event.assert_not_called()
    db.rollback.assert_called_once()


def test_integrity_recovery_hit_rejects_changed_request():
    db = Mock()
    order_repository = Mock()
    order_repository.get_by_client_order_id_for_user.side_effect = [
        None,
        make_existing_order(total_volume=3),
    ]
    order_repository.get_by_client_order_id.return_value = None
    order_repository.create.side_effect = IntegrityError(
        "insert order",
        {},
        Exception("unique"),
    )
    account_repository = Mock()
    account_repository.get_owned_account.return_value = make_account()
    account_repository.get_owned_account_for_update.return_value = (
        make_account()
    )
    rule_query_service = Mock()
    rule_query_service.get_order_rules.return_value = make_rules()
    outbox_repository = Mock()
    service = make_service(
        order_repository=order_repository,
        account_repository=account_repository,
        rule_query_service=rule_query_service,
        outbox_repository=outbox_repository,
    )

    with pytest.raises(ResourceConflictError) as exc_info:
        service.create_order(
            db,
            make_request(),
            access_scope=AccountAccessScope.for_user("U001"),
        )

    assert exc_info.value.error_code == "IDEMPOTENCY_KEY_REUSED"
    db.rollback.assert_called_once()
    db.commit.assert_not_called()
    outbox_repository.create_event.assert_not_called()


def test_missing_account_rolls_back_and_does_not_create_order():
    db = Mock()
    order_repository = Mock()
    order_repository.get_by_client_order_id.return_value = None
    account_repository = Mock()
    account_repository.get_by_account_id_for_update.return_value = None
    rule_query_service = Mock()
    rule_query_service.get_order_rules.return_value = make_rules()
    service = make_service(
        order_repository=order_repository,
        account_repository=account_repository,
        rule_query_service=rule_query_service,
    )

    with pytest.raises(ResourceNotFoundError):
        service.create_order(db=db, request=make_request())

    order_repository.create.assert_not_called()
    db.rollback.assert_called_once_with()


def test_freeze_failure_does_not_create_order():
    db = Mock()
    order_repository = Mock()
    order_repository.get_by_client_order_id.return_value = None
    account_repository = Mock()
    account_repository.get_by_account_id_for_update.return_value = (
        make_account(available_cash=Decimal("1"))
    )
    rule_query_service = Mock()
    rule_query_service.get_order_rules.return_value = make_rules()
    service = make_service(
        order_repository=order_repository,
        account_repository=account_repository,
        rule_query_service=rule_query_service,
    )

    with pytest.raises(BusinessRuleError) as exc_info:
        service.create_order(db=db, request=make_request())

    assert exc_info.value.error_code == "INSUFFICIENT_AVAILABLE_CASH"
    order_repository.create.assert_not_called()
    db.rollback.assert_called_once_with()


@pytest.mark.parametrize(
    "error_code",
    [
        "INSTRUMENT_NOT_FOUND",
        "INSTRUMENT_INACTIVE",
        "MARGIN_RULE_NOT_FOUND",
        "FEE_RULE_NOT_FOUND",
    ],
)
def test_reference_rule_failure_occurs_before_account_lock(error_code):
    db = Mock()
    order_repository = Mock()
    order_repository.get_by_client_order_id.return_value = None
    account_repository = Mock()
    account_repository.get_by_account_id_for_update.return_value = (
        make_account()
    )
    rule_query_service = Mock()
    rule_query_service.get_order_rules.side_effect = BusinessRuleError(
        "规则不可用",
        error_code=error_code,
    )
    service = make_service(
        order_repository=order_repository,
        account_repository=account_repository,
        rule_query_service=rule_query_service,
    )

    with pytest.raises(BusinessRuleError) as exc_info:
        service.create_order(db=db, request=make_request())

    assert exc_info.value.error_code == error_code
    account_repository.get_by_account_id_for_update.assert_not_called()
    order_repository.create.assert_not_called()
    db.rollback.assert_called_once_with()


class FailingOrderRepository(OrderRepository):
    @staticmethod
    def create(db: Session, **kwargs):
        raise OperationalError("insert order", {}, Exception("failed"))


class FailingOutboxRepository(OutboxRepository):
    @staticmethod
    def create_event(db: Session, **kwargs):
        raise OperationalError("insert outbox", {}, Exception("failed"))


def test_order_create_failure_rolls_back_real_account_freeze():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Account.__table__.create(engine)
    Order.__table__.create(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory() as setup_db:
        setup_db.add(
            Account(
                account_id="A001",
                user_id="U001",
                account_name="test",
                account_type="FUTURES",
                initial_cash=Decimal("100000"),
                cash_balance=Decimal("100000"),
                available_cash=Decimal("100000"),
                frozen_cash=Decimal("0"),
                equity=Decimal("100000"),
                used_margin=Decimal("0"),
                frozen_margin=Decimal("0"),
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                daily_pnl=Decimal("0"),
                used_commission=Decimal("0"),
                frozen_commission=Decimal("0"),
                risk_ratio=Decimal("0"),
                status="NORMAL",
                trading_day=TRADING_DAY,
            )
        )
        setup_db.commit()

    rule_query_service = Mock()
    rule_query_service.get_order_rules.return_value = make_rules()
    service = make_service(
        order_repository=FailingOrderRepository(),
        account_repository=AccountRepository(),
        rule_query_service=rule_query_service,
    )

    with session_factory() as db:
        with pytest.raises(DataAccessError):
            service.create_order(db=db, request=make_request())

    with session_factory() as verify_db:
        account = verify_db.scalar(
            select(Account).where(Account.account_id == "A001")
        )
        order_count = verify_db.scalar(select(func.count(Order.id)))
        assert account.available_cash == Decimal("100000.000000")
        assert account.frozen_margin == Decimal("0.000000")
        assert account.frozen_commission == Decimal("0.000000")
        assert order_count == 0


def test_outbox_create_failure_rolls_back_order_and_account_freeze():
    """Outbox 写入失败时，账户冻结和订单记录必须一起回滚。"""

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Account.__table__.create(engine)
    Order.__table__.create(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory() as setup_db:
        setup_db.add(
            Account(
                account_id="A001",
                user_id="U001",
                account_name="test",
                account_type="FUTURES",
                initial_cash=Decimal("100000"),
                cash_balance=Decimal("100000"),
                available_cash=Decimal("100000"),
                frozen_cash=Decimal("0"),
                equity=Decimal("100000"),
                used_margin=Decimal("0"),
                frozen_margin=Decimal("0"),
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                daily_pnl=Decimal("0"),
                used_commission=Decimal("0"),
                frozen_commission=Decimal("0"),
                risk_ratio=Decimal("0"),
                status="NORMAL",
                trading_day=TRADING_DAY,
            )
        )
        setup_db.commit()

    rule_query_service = Mock()
    rule_query_service.get_order_rules.return_value = make_rules()
    service = make_service(
        order_repository=OrderRepository(),
        account_repository=AccountRepository(),
        rule_query_service=rule_query_service,
        outbox_repository=FailingOutboxRepository(),
    )

    with session_factory() as db:
        with pytest.raises(DataAccessError):
            service.create_order(db=db, request=make_request())

    with session_factory() as verify_db:
        account = verify_db.scalar(
            select(Account).where(Account.account_id == "A001")
        )
        order_count = verify_db.scalar(select(func.count(Order.id)))
        assert account.available_cash == Decimal("100000.000000")
        assert account.frozen_margin == Decimal("0.000000")
        assert account.frozen_commission == Decimal("0.000000")
        assert order_count == 0


def test_account_repository_uses_select_for_update():
    db = Mock()
    AccountRepository.get_by_account_id_for_update(db, "A001")

    statement = db.scalar.call_args.args[0]
    assert statement._for_update_arg is not None
    assert statement.get_execution_options()["populate_existing"] is True


def test_owned_account_repository_puts_user_scope_in_for_update_sql():
    db = Mock()

    AccountRepository.get_owned_account_for_update(
        db,
        account_id="A001",
        user_id="U001",
    )

    statement = db.scalar.call_args.args[0]
    sql = str(statement).lower()
    assert statement._for_update_arg is not None
    assert statement.get_execution_options()["populate_existing"] is True
    assert "account.account_id" in sql
    assert "account.user_id" in sql
