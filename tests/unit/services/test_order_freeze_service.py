from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.common.exceptions import BusinessRuleError, ResourceNotFoundError
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
