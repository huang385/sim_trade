from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.common.exceptions import (
    BusinessRuleError,
    BusinessValidationError,
    DataAccessError,
    ResourceNotFoundError,
)
from app.services.order_freeze_service import OrderFreezeService


def make_account(**overrides):
    values = {
        "status": "NORMAL",
        "available_cash": Decimal("100000.000000"),
        "frozen_margin": Decimal("0.000000"),
        "frozen_commission": Decimal("0.000000"),
        "cash_balance": Decimal("100000.000000"),
        "equity": Decimal("100000.000000"),
        "used_margin": Decimal("0.000000"),
        "used_commission": Decimal("0.000000"),
        "realized_pnl": Decimal("0.000000"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_freezes_margin_and_commission_only():
    account = make_account()

    OrderFreezeService.freeze_open_order(
        account=account,
        frozen_margin=Decimal("8400"),
        frozen_commission=Decimal("6"),
    )

    assert account.available_cash == Decimal("91594.000000")
    assert account.frozen_margin == Decimal("8400.000000")
    assert account.frozen_commission == Decimal("6.000000")
    assert account.cash_balance == Decimal("100000.000000")
    assert account.equity == Decimal("100000.000000")
    assert account.used_margin == Decimal("0.000000")
    assert account.used_commission == Decimal("0.000000")
    assert account.realized_pnl == Decimal("0.000000")


def test_rejects_insufficient_cash_without_mutation():
    account = make_account(available_cash=Decimal("8405"))

    with pytest.raises(BusinessRuleError) as exc_info:
        OrderFreezeService.freeze_open_order(
            account=account,
            frozen_margin=Decimal("8400"),
            frozen_commission=Decimal("6"),
        )

    assert exc_info.value.error_code == "INSUFFICIENT_AVAILABLE_CASH"
    assert account.available_cash == Decimal("8405")
    assert account.frozen_margin == Decimal("0.000000")
    assert account.frozen_commission == Decimal("0.000000")


def test_futures_open_uses_lower_risk_available_cash():
    account = make_account(
        available_cash=Decimal("100000"),
        risk_available_cash=Decimal("100"),
    )

    with pytest.raises(BusinessRuleError) as exc_info:
        OrderFreezeService.freeze_open_order(
            account=account,
            frozen_margin=Decimal("100"),
            frozen_commission=Decimal("1"),
        )

    assert exc_info.value.error_code == "INSUFFICIENT_RISK_AVAILABLE_CASH"
    assert account.available_cash == Decimal("100000")
    assert account.risk_available_cash == Decimal("100")


def test_close_commission_remains_allowed_during_margin_deficit():
    account = make_account(
        available_cash=Decimal("100"),
        risk_available_cash=Decimal("-500"),
    )

    OrderFreezeService.freeze_close_order_commission(
        account=account,
        frozen_commission=Decimal("6"),
    )

    assert account.available_cash == Decimal("94.000000")
    assert account.risk_available_cash == Decimal("-506.000000")
    assert account.frozen_commission == Decimal("6.000000")


def test_rejects_missing_account():
    with pytest.raises(ResourceNotFoundError) as exc_info:
        OrderFreezeService.freeze_open_order(
            account=None,
            frozen_margin=Decimal("1"),
            frozen_commission=Decimal("1"),
        )

    assert exc_info.value.error_code == "ACCOUNT_NOT_FOUND"


@pytest.mark.parametrize("status", ["DISABLED", "LIQUIDATION"])
def test_rejects_non_tradable_account(status):
    with pytest.raises(BusinessRuleError) as exc_info:
        OrderFreezeService.freeze_open_order(
            account=make_account(status=status),
            frozen_margin=Decimal("1"),
            frozen_commission=Decimal("1"),
        )

    assert exc_info.value.error_code == "ACCOUNT_NOT_TRADABLE"


@pytest.mark.parametrize("status", ["NORMAL", "DISABLED", "LIQUIDATION"])
def test_releases_cancel_resources_for_every_account_status(status):
    account = make_account(
        status=status,
        available_cash=Decimal("79900"),
        frozen_margin=Decimal("20000"),
        frozen_commission=Decimal("100"),
        used_margin=Decimal("6000"),
        used_commission=Decimal("30"),
    )
    unchanged = (
        account.cash_balance,
        account.equity,
        account.used_margin,
        account.used_commission,
        account.realized_pnl,
    )

    OrderFreezeService.release_open_order_frozen_resources(
        account=account,
        frozen_margin=Decimal("14000"),
        frozen_commission=Decimal("70"),
    )

    assert account.available_cash == Decimal("93970.000000")
    assert account.frozen_margin == Decimal("6000.000000")
    assert account.frozen_commission == Decimal("30.000000")
    assert (
        account.cash_balance,
        account.equity,
        account.used_margin,
        account.used_commission,
        account.realized_pnl,
    ) == unchanged


@pytest.mark.parametrize(
    ("margin", "commission", "error_code"),
    [
        (Decimal("-1"), Decimal("0"), "INVALID_RELEASED_MARGIN"),
        (Decimal("0"), Decimal("-1"), "INVALID_RELEASED_COMMISSION"),
    ],
)
def test_release_rejects_negative_amount_without_mutation(
    margin,
    commission,
    error_code,
):
    account = make_account(
        available_cash=Decimal("10"),
        frozen_margin=Decimal("5"),
        frozen_commission=Decimal("2"),
    )

    with pytest.raises(BusinessValidationError) as exc_info:
        OrderFreezeService.release_open_order_frozen_resources(
            account=account,
            frozen_margin=margin,
            frozen_commission=commission,
        )

    assert exc_info.value.error_code == error_code
    assert account.available_cash == Decimal("10")
    assert account.frozen_margin == Decimal("5")
    assert account.frozen_commission == Decimal("2")


@pytest.mark.parametrize(
    ("account_overrides", "margin", "commission"),
    [
        ({"frozen_margin": Decimal("4")}, Decimal("5"), Decimal("1")),
        ({"frozen_commission": Decimal("0")}, Decimal("1"), Decimal("1")),
    ],
)
def test_release_rejects_inconsistent_frozen_resources_without_negative_balance(
    account_overrides,
    margin,
    commission,
):
    values = {
        "available_cash": Decimal("10"),
        "frozen_margin": Decimal("5"),
        "frozen_commission": Decimal("2"),
    }
    values.update(account_overrides)
    account = make_account(**values)
    before = (
        account.available_cash,
        account.frozen_margin,
        account.frozen_commission,
    )

    with pytest.raises(DataAccessError) as exc_info:
        OrderFreezeService.release_open_order_frozen_resources(
            account=account,
            frozen_margin=margin,
            frozen_commission=commission,
        )

    assert (
        exc_info.value.error_code
        == "CANCEL_FROZEN_RESOURCE_INCONSISTENT"
    )
    assert (
        account.available_cash,
        account.frozen_margin,
        account.frozen_commission,
    ) == before
