from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError

from app.common.exceptions import (
    AuthorizationError,
    DataAccessError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from app.schemas.order_schema import OrderCancelRequest
from app.repositories.order_repository import OrderRepository
from app.services.order_cancellation_service import OrderCancellationService
from app.services.order_freeze_service import OrderFreezeService


FIXED_TIME = datetime(2026, 7, 24, 1, 2, 3, tzinfo=timezone.utc)


def make_order(**overrides):
    values = {
        "order_id": "O-1",
        "client_order_id": "C-1",
        "account_id": "A001",
        "order_book_id": "RB2610",
        "exchange_id": "SHFE",
        "symbol": "RB2610",
        "trading_day": date(2026, 7, 24),
        "direction": "BUY",
        "offset_flag": "OPEN",
        "order_type": "LIMIT",
        "total_volume": 10,
        "traded_volume": 0,
        "remaining_volume": 10,
        "cancelled_volume": 0,
        "average_price": None,
        "status": "ACCEPTED",
        "submit_status": "ACCEPTED",
        "frozen_margin": Decimal("20000"),
        "frozen_commission": Decimal("100"),
        "frozen_position_volume": 0,
        "cancelled_at": None,
        "accepted_at": datetime(2026, 7, 24, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 7, 24, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_account(**overrides):
    values = {
        "account_id": "A001",
        "user_id": "U001",
        "status": "NORMAL",
        "available_cash": Decimal("79900"),
        "frozen_margin": Decimal("20000"),
        "frozen_commission": Decimal("100"),
        "cash_balance": Decimal("100000"),
        "equity": Decimal("100000"),
        "used_margin": Decimal("0"),
        "used_commission": Decimal("0"),
        "realized_pnl": Decimal("0"),
        "unrealized_pnl": Decimal("0"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_service(order, account=None):
    order_repository = Mock()
    order_repository.get_by_order_id_for_update.return_value = order
    account_repository = Mock()
    account_repository.get_by_account_id_for_update.return_value = (
        account if account is not None else make_account()
    )
    outbox_repository = Mock()
    service = OrderCancellationService(
        order_repository=order_repository,
        account_repository=account_repository,
        freeze_service=OrderFreezeService(),
        outbox_repository=outbox_repository,
        event_id_factory=lambda: "EVT-CANCEL-1",
        time_provider=lambda: FIXED_TIME,
    )
    return service, order_repository, account_repository, outbox_repository


def cancel(service, db, *, account_id=" A001 ", order_id=" O-1 "):
    return service.cancel_order(
        db=db,
        order_id=order_id,
        request=OrderCancelRequest(account_id=account_id),
    )


def test_accepted_order_cancel_releases_only_frozen_resources_and_writes_outbox():
    order = make_order()
    account = make_account()
    unchanged_account = (
        account.cash_balance,
        account.equity,
        account.used_margin,
        account.used_commission,
    )
    service, _, _, outbox = make_service(order, account)
    db = Mock()

    result = cancel(service, db)

    assert result is order
    assert order.status == "CANCELLED"
    assert (order.traded_volume, order.remaining_volume, order.cancelled_volume) == (
        0,
        0,
        10,
    )
    assert order.frozen_margin == Decimal("0.000000")
    assert order.frozen_commission == Decimal("0.000000")
    assert order.cancelled_at == order.updated_at == FIXED_TIME
    assert order.submit_status == "ACCEPTED"
    assert order.average_price is None
    assert account.available_cash == Decimal("100000.000000")
    assert account.frozen_margin == Decimal("0.000000")
    assert account.frozen_commission == Decimal("0.000000")
    assert (
        account.cash_balance,
        account.equity,
        account.used_margin,
        account.used_commission,
    ) == unchanged_account
    event = outbox.create_event.call_args.kwargs
    assert event["event_type"] == "ORDER_CANCELLED"
    assert event["payload"]["released_margin"] == "20000.000000"
    assert event["payload"]["released_commission"] == "100.000000"
    assert event["payload"]["cancelled_at"] == FIXED_TIME.isoformat()
    db.commit.assert_called_once()
    db.refresh.assert_called_once_with(order)


def test_partially_filled_order_cancels_only_remaining_and_keeps_trade_values():
    order = make_order(
        status="PARTIALLY_FILLED",
        traded_volume=3,
        remaining_volume=7,
        average_price=Decimal("3499"),
        frozen_margin=Decimal("14000"),
        frozen_commission=Decimal("70"),
    )
    account = make_account(
        frozen_margin=Decimal("14000"),
        frozen_commission=Decimal("70"),
        used_margin=Decimal("6000"),
        used_commission=Decimal("30"),
    )
    service, _, _, outbox = make_service(order, account)

    cancel(service, Mock())

    assert order.status == "PARTIALLY_CANCELLED"
    assert (order.traded_volume, order.remaining_volume, order.cancelled_volume) == (
        3,
        0,
        7,
    )
    assert order.average_price == Decimal("3499")
    assert account.used_margin == Decimal("6000")
    assert account.used_commission == Decimal("30")
    assert account.available_cash == Decimal("93970.000000")
    assert outbox.create_event.call_args.kwargs["event_type"] == (
        "ORDER_PARTIALLY_CANCELLED"
    )


@pytest.mark.parametrize("status", ["CANCELLED", "PARTIALLY_CANCELLED"])
def test_repeated_cancel_ends_transaction_without_second_business_change(status):
    original_time = datetime(2026, 7, 24, 1, tzinfo=timezone.utc)
    order = make_order(
        status=status,
        remaining_volume=0,
        cancelled_volume=10,
        frozen_margin=Decimal("0"),
        frozen_commission=Decimal("0"),
        cancelled_at=original_time,
    )
    service, _, account_repository, outbox = make_service(order)
    service.freeze_service = Mock(spec=OrderFreezeService)
    db = Mock()

    assert cancel(service, db) is order

    assert order.cancelled_volume == 10
    assert order.remaining_volume == 0
    assert order.frozen_margin == Decimal("0")
    assert order.frozen_commission == Decimal("0")
    assert order.cancelled_at == original_time
    account_repository.get_by_account_id_for_update.assert_called_once()
    service.freeze_service.release_open_order_frozen_resources.assert_not_called()
    outbox.create_event.assert_not_called()
    db.expunge.assert_called_once_with(order)
    db.commit.assert_called_once()
    db.refresh.assert_not_called()
    db.rollback.assert_not_called()


def test_actual_order_account_is_authorized_before_idempotent_return():
    """请求体伪造自有账户时，也必须按订单真实账户拒绝且不返回幂等结果。"""

    order = make_order(
        account_id="B001",
        status="CANCELLED",
        remaining_volume=0,
        cancelled_volume=10,
        frozen_margin=Decimal("0"),
        frozen_commission=Decimal("0"),
    )
    service, _, account_repository, outbox = make_service(order)
    checker = Mock(
        side_effect=AuthorizationError(
            "无权访问该交易账户",
            error_code="ACCOUNT_ACCESS_DENIED",
        )
    )
    db = Mock()

    with pytest.raises(AuthorizationError):
        service.cancel_order(
            db=db,
            order_id="O-1",
            # 攻击者故意提交自己拥有的A001，但订单实际属于B001。
            request=OrderCancelRequest(account_id="A001"),
            account_access_checker=checker,
        )

    locked_account = (
        account_repository.get_by_account_id_for_update.return_value
    )
    checker.assert_called_once_with(locked_account)
    account_repository.get_by_account_id_for_update.assert_called_once()
    outbox.create_event.assert_not_called()
    db.commit.assert_not_called()
    db.rollback.assert_called_once()


def test_normal_user_scopes_order_and_account_locks_in_sql():
    order = make_order(
        status="CANCELLED",
        remaining_volume=0,
        cancelled_volume=10,
        frozen_margin=Decimal("0"),
        frozen_commission=Decimal("0"),
    )
    account = make_account()
    order_repository = Mock()
    account_repository = Mock()
    order_repository.get_by_order_id_for_user_for_update.return_value = order
    account_repository.get_owned_account_for_update.return_value = account
    service = OrderCancellationService(
        order_repository=order_repository,
        account_repository=account_repository,
        outbox_repository=Mock(),
    )
    db = Mock()

    service.cancel_order(
        db=db,
        order_id="O-1",
        request=OrderCancelRequest(account_id="A001"),
        account_owner_user_id="U001",
        conceal_resource_existence=True,
    )

    order_repository.get_by_order_id_for_user_for_update.assert_called_once_with(
        db,
        order_id="O-1",
        user_id="U001",
    )
    order_repository.get_by_order_id_for_update.assert_not_called()
    account_repository.get_owned_account_for_update.assert_called_once()
    account_repository.get_by_account_id_for_update.assert_not_called()


def test_unauthorized_cancel_does_not_lock_foreign_order_or_account():
    order_repository = Mock()
    account_repository = Mock()
    order_repository.get_by_order_id_for_user_for_update.return_value = None
    service = OrderCancellationService(
        order_repository=order_repository,
        account_repository=account_repository,
        outbox_repository=Mock(),
    )

    with pytest.raises(ResourceNotFoundError) as exc_info:
        service.cancel_order(
            db=Mock(),
            order_id="O-FOREIGN",
            request=OrderCancelRequest(account_id="A-FOREIGN"),
            account_owner_user_id="U001",
            conceal_resource_existence=True,
        )

    assert exc_info.value.error_code == "RESOURCE_NOT_FOUND"
    order_repository.get_by_order_id_for_update.assert_not_called()
    account_repository.get_owned_account_for_update.assert_not_called()
    account_repository.get_by_account_id_for_update.assert_not_called()


def test_user_scoped_order_lock_only_locks_orders_table():
    db = Mock()

    OrderRepository.get_by_order_id_for_user_for_update(
        db,
        order_id="O-1",
        user_id="U001",
    )

    statement = db.scalar.call_args.args[0]
    sql = str(
        statement.compile(dialect=postgresql.dialect())
    ).lower()
    assert "join account" in sql
    assert "account.user_id" in sql
    assert "for update of orders" in sql


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("order_type", "MARKET"),
        ("offset_flag", "UNKNOWN"),
    ],
)
def test_non_limit_or_unsupported_offset_is_rejected_after_account_lock(
    field,
    value,
):
    order = make_order(**{field: value})
    service, _, account_repository, outbox = make_service(order)
    service.freeze_service = Mock(spec=OrderFreezeService)
    db = Mock()

    with pytest.raises(ResourceConflictError) as exc_info:
        cancel(service, db)

    assert exc_info.value.error_code == "ORDER_NOT_CANCELLABLE"
    account_repository.get_by_account_id_for_update.assert_called_once()
    service.freeze_service.release_open_order_frozen_resources.assert_not_called()
    outbox.create_event.assert_not_called()
    db.commit.assert_not_called()
    db.rollback.assert_called_once()


def test_open_order_with_frozen_position_is_consistency_error_without_changes():
    order = make_order(frozen_position_volume=2)
    account = make_account()
    original_order = (
        order.status,
        order.remaining_volume,
        order.cancelled_volume,
        order.frozen_margin,
        order.frozen_commission,
        order.cancelled_at,
        order.updated_at,
    )
    original_account = (
        account.available_cash,
        account.frozen_margin,
        account.frozen_commission,
    )
    service, _, account_repository, outbox = make_service(order, account)
    service.freeze_service = Mock(spec=OrderFreezeService)
    db = Mock()

    with pytest.raises(DataAccessError) as exc_info:
        cancel(service, db)

    assert exc_info.value.error_code == "CANCEL_ORDER_STATE_INCONSISTENT"
    assert (
        order.status,
        order.remaining_volume,
        order.cancelled_volume,
        order.frozen_margin,
        order.frozen_commission,
        order.cancelled_at,
        order.updated_at,
    ) == original_order
    assert (
        account.available_cash,
        account.frozen_margin,
        account.frozen_commission,
    ) == original_account
    account_repository.get_by_account_id_for_update.assert_called_once()
    service.freeze_service.release_open_order_frozen_resources.assert_not_called()
    outbox.create_event.assert_not_called()
    db.commit.assert_not_called()
    db.rollback.assert_called_once()


@pytest.mark.parametrize("status", ["FILLED", "REJECTED", "NEW"])
def test_non_cancellable_status_returns_conflict(status):
    service, _, account_repository, outbox = make_service(
        make_order(status=status)
    )
    db = Mock()

    with pytest.raises(ResourceConflictError) as exc_info:
        cancel(service, db)

    assert exc_info.value.error_code == "ORDER_NOT_CANCELLABLE"
    account_repository.get_by_account_id_for_update.assert_called_once()
    outbox.create_event.assert_not_called()
    db.rollback.assert_called_once()


def test_missing_order_and_account_mismatch_return_clear_errors():
    db = Mock()
    service, _, _, _ = make_service(None)
    with pytest.raises(ResourceNotFoundError) as missing:
        cancel(service, db)
    assert missing.value.error_code == "ORDER_NOT_FOUND"

    service, _, _, _ = make_service(make_order(account_id="A002"))
    with pytest.raises(ResourceConflictError) as mismatch:
        cancel(service, Mock())
    assert mismatch.value.error_code == "ORDER_ACCOUNT_MISMATCH"


def test_active_order_with_zero_remaining_is_consistency_error():
    service, _, account_repository, _ = make_service(
        make_order(remaining_volume=0)
    )

    with pytest.raises(DataAccessError) as exc_info:
        cancel(service, Mock())

    assert exc_info.value.error_code == "CANCEL_ORDER_STATE_INCONSISTENT"
    account_repository.get_by_account_id_for_update.assert_called_once()


def test_frozen_resource_inconsistency_rolls_back_without_outbox():
    order = make_order()
    account = make_account(frozen_margin=Decimal("1"))
    service, _, _, outbox = make_service(order, account)
    db = Mock()

    with pytest.raises(DataAccessError) as exc_info:
        cancel(service, db)

    assert exc_info.value.error_code == "CANCEL_FROZEN_RESOURCE_INCONSISTENT"
    outbox.create_event.assert_not_called()
    db.rollback.assert_called_once()


def test_outbox_failure_and_commit_failure_roll_back():
    service, _, _, outbox = make_service(make_order())
    outbox.create_event.side_effect = RuntimeError("outbox failed")
    db = Mock()
    with pytest.raises(RuntimeError, match="outbox failed"):
        cancel(service, db)
    db.rollback.assert_called_once()

    service, _, _, _ = make_service(make_order())
    db = Mock()
    db.commit.side_effect = OperationalError(
        "commit",
        {},
        Exception("failed"),
    )
    with pytest.raises(DataAccessError) as exc_info:
        cancel(service, db)
    assert exc_info.value.error_code == "ORDER_CANCEL_FAILED"
    db.rollback.assert_called_once()


def test_lock_order_is_order_then_account():
    calls: list[str] = []
    order = make_order()
    account = make_account()
    order_repository = Mock()
    account_repository = Mock()
    order_repository.get_by_order_id_for_update.side_effect = (
        lambda *_args: calls.append("ORDER") or order
    )
    account_repository.get_by_account_id_for_update.side_effect = (
        lambda *_args: calls.append("ACCOUNT") or account
    )
    service = OrderCancellationService(
        order_repository=order_repository,
        account_repository=account_repository,
        outbox_repository=Mock(),
        event_id_factory=lambda: "EVT-1",
        time_provider=lambda: FIXED_TIME,
    )

    cancel(service, Mock())

    assert calls == ["ORDER", "ACCOUNT"]


def test_close_cancel_uses_complete_stable_lock_order():
    """平仓撤单必须按统一顺序加锁，避免与成交结算形成反向等待。"""

    calls: list[str] = []
    order = make_order(
        direction="SELL",
        offset_flag="CLOSE_TODAY",
        frozen_margin=Decimal("0"),
        frozen_commission=Decimal("10"),
        frozen_position_volume=10,
    )
    account = make_account(
        frozen_margin=Decimal("0"),
        frozen_commission=Decimal("10"),
    )
    position = SimpleNamespace(
        position_id="P-1",
        direction="LONG",
    )
    order_repository = Mock()
    account_repository = Mock()
    position_repository = Mock()
    allocation_repository = Mock()
    order_repository.get_by_order_id_for_update.side_effect = (
        lambda *_args: calls.append("ORDER") or order
    )
    account_repository.get_by_account_id_for_update.side_effect = (
        lambda *_args: calls.append("ACCOUNT") or account
    )
    position_repository.get_for_update.side_effect = (
        lambda *_args, **_kwargs: calls.append("POSITION") or position
    )
    position_repository.list_details_by_order_for_update.side_effect = (
        lambda *_args, **_kwargs: calls.append("POSITION_DETAIL") or []
    )
    allocation_repository.list_by_order_for_update.side_effect = (
        lambda *_args, **_kwargs: calls.append("ALLOCATION") or []
    )
    service = OrderCancellationService(
        order_repository=order_repository,
        account_repository=account_repository,
        position_repository=position_repository,
        allocation_repository=allocation_repository,
        outbox_repository=Mock(),
        event_id_factory=lambda: "EVT-1",
        time_provider=lambda: FIXED_TIME,
    )

    with pytest.raises(DataAccessError) as exc_info:
        cancel(service, Mock())

    assert exc_info.value.error_code == "CANCEL_POSITION_INCONSISTENT"
    assert calls == [
        "ORDER",
        "ACCOUNT",
        "POSITION",
        "POSITION_DETAIL",
        "ALLOCATION",
    ]
