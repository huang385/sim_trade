from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.common.exceptions import (
    BusinessRuleError,
    DataAccessError,
    ResourceNotFoundError,
)
from app.enums.order_enums import OrderDirection
from app.models.account import Account
from app.models.order import Order
from app.repositories.account_repository import AccountRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.outbox_repository import OutboxRepository
from app.schemas.order_schema import OrderCreateRequest
from app.services.fee_calculator import FeeCalculator
from app.services.margin_calculator import MarginCalculator
from app.services.order_freeze_service import OrderFreezeService
from app.services.order_service import OrderService
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
        "available_cash": Decimal("100000"),
        "frozen_margin": Decimal("0"),
        "frozen_commission": Decimal("0"),
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
    event_args = outbox_repository.create_event.call_args.kwargs
    assert event_args["aggregate_type"] == "ORDER"
    assert event_args["aggregate_id"] == "O20260717000001"
    assert event_args["event_type"] == "ORDER_ACCEPTED"
    assert event_args["payload"]["limit_price"] == "3500.000000"
    assert event_args["payload"]["direction"] == direction.value
    db.commit.assert_called_once_with()
    db.refresh.assert_called_once_with(created_order)


def test_duplicate_client_order_id_returns_existing_without_freeze():
    db = Mock()
    existing = SimpleNamespace(order_id="EXISTING")
    order_repository = Mock()
    order_repository.get_by_client_order_id.return_value = existing
    account_repository = Mock()
    rule_query_service = Mock()
    service = make_service(
        order_repository=order_repository,
        account_repository=account_repository,
        rule_query_service=rule_query_service,
    )

    result = service.create_order(db=db, request=make_request())

    assert result is existing
    account_repository.get_by_account_id_for_update.assert_not_called()
    order_repository.create.assert_not_called()
    db.commit.assert_not_called()


@pytest.mark.parametrize(
    ("direction", "offset_flag", "position_direction", "commission"),
    [
        ("SELL", "CLOSE_TODAY", "LONG", Decimal("12.000000")),
        ("BUY", "CLOSE_TODAY", "SHORT", Decimal("12.000000")),
        ("SELL", "CLOSE_YESTERDAY", "LONG", Decimal("6.000000")),
        ("BUY", "CLOSE", "SHORT", Decimal("6.000000")),
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
        frozen_volume=0,
        available_volume=5,
        updated_at=None,
    )
    position_repository = Mock()
    position_repository.get_for_update.return_value = position
    position_repository.list_details_for_update.return_value = [detail]
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
        outbox_repository.create_event.call_args.kwargs["payload"][
            "frozen_position_volume"
        ]
        == 2
    )


def test_duplicate_detected_after_account_lock_does_not_freeze_twice():
    db = Mock()
    existing = SimpleNamespace(order_id="EXISTING")
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
    db.commit.assert_not_called()


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
def test_reference_rule_failure_prevents_account_lock(error_code):
    db = Mock()
    order_repository = Mock()
    order_repository.get_by_client_order_id.return_value = None
    account_repository = Mock()
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
                user_id=None,
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
                user_id=None,
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
