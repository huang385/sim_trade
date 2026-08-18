from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.common.exceptions import DataAccessError
from app.matching.cash_security import (
    CashSecurityMarketSnapshot,
    CashSecurityMatchingStrategy,
    CashSecurityOrderSnapshot,
)
from app.services.cash_security_position_service import CashSecurityPositionService


def test_cash_buy_matches_best_ask_and_caps_to_book_volume():
    result = CashSecurityMatchingStrategy().match(
        CashSecurityOrderSnapshot(
            order_id="S-1",
            instrument_type="STOCK",
            direction="BUY",
            limit_price=Decimal("10.00"),
            remaining_volume=300,
        ),
        CashSecurityMarketSnapshot(
            bid_price_1=Decimal("9.99"),
            bid_volume_1=80,
            ask_price_1=Decimal("10.00"),
            ask_volume_1=120,
        ),
    )
    assert result.matched is True
    assert result.fill_price == Decimal("10.00")
    assert result.fill_volume == 120


def test_cash_sell_does_not_match_when_bid_is_below_limit():
    result = CashSecurityMatchingStrategy().match(
        CashSecurityOrderSnapshot(
            order_id="B-1",
            instrument_type="CONVERTIBLE_BOND",
            direction="SELL",
            limit_price=Decimal("101"),
            remaining_volume=10,
        ),
        CashSecurityMarketSnapshot(
            bid_price_1=Decimal("100"),
            bid_volume_1=100,
            ask_price_1=Decimal("101"),
            ask_volume_1=100,
        ),
    )
    assert result.matched is False
    assert result.reason == "SELL_LIMIT_NOT_REACHED"


def _position(**overrides):
    values = dict(
        total_volume=100,
        today_volume=20,
        yesterday_volume=80,
        frozen_volume=20,
        settlement_locked_volume=20,
        available_volume=60,
        position_cost=Decimal("1000"),
        average_open_price=Decimal("10"),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_stock_sell_cannot_consume_today_locked_volume():
    with pytest.raises(DataAccessError, match="锁定持仓"):
        CashSecurityPositionService.apply_sell(
            _position(yesterday_volume=10, frozen_volume=20),
            instrument_type="STOCK",
            volume=20,
        )


def test_convertible_bond_sell_can_consume_today_volume():
    position = _position(today_volume=20, yesterday_volume=0, frozen_volume=20)
    cost = CashSecurityPositionService.apply_sell(
        position, instrument_type="CONVERTIBLE_BOND", volume=20
    )
    assert cost == Decimal("200.000000")
    assert position.today_volume == 0
    assert position.frozen_volume == 0
    assert position.total_volume == 80
