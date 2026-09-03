from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.common.exceptions import (
    BusinessRuleError,
    BusinessValidationError,
    DataAccessError,
)
from app.enums.market_feed_enums import (
    MarketFeedDomain,
    resolve_market_feed_domain,
)
from app.schemas.instrument_schema import InstrumentCreate
from app.schemas.order_schema import EtfOrderCreateRequest
from app.services.cash_security_position_service import CashSecurityPositionService
from app.services.stock_order_validation_service import EtfTradingPolicy


def _position(**updates):
    values = {
        "direction": "LONG",
        "total_volume": 100,
        "today_volume": 0,
        "yesterday_volume": 100,
        "available_volume": 100,
        "frozen_volume": 0,
        "settlement_locked_volume": 0,
        "position_cost": Decimal("100"),
        "daily_pnl_base_cost": Decimal("100"),
        "yesterday_pnl_base_cost": Decimal("100"),
        "today_pnl_base_cost": Decimal("0"),
        "daily_pnl_base_established": True,
        "average_open_price": Decimal("1"),
    }
    values.update(updates)
    return SimpleNamespace(**values)


def _request(*, direction="BUY", volume=100):
    return EtfOrderCreateRequest(
        client_order_id="ETF-1",
        account_id="A1",
        exchange_id="SSE",
        symbol="510300",
        direction=direction,
        order_type="LIMIT",
        limit_price="4.001",
        volume=volume,
    )


def test_etf_instrument_requires_fund_reference_fields():
    item = InstrumentCreate(
        order_book_id="510300.XSHG",
        symbol="510300",
        exchange_id="SSE",
        market_type="FUND",
        instrument_type="ETF",
        fund_type="StockIndex",
        market_tplus=1,
        round_lot=100,
        contract_multiplier="1",
        price_tick="0.001",
    )
    assert item.market_tplus == 1
    assert item.round_lot == 100

    with pytest.raises(ValidationError):
        InstrumentCreate(
            order_book_id="510300.XSHG",
            symbol="510300",
            exchange_id="SSE",
            market_type="FUND",
            instrument_type="ETF",
            contract_multiplier="1",
            price_tick="0.001",
        )


def test_etf_routes_to_shared_securities_market_domain():
    assert resolve_market_feed_domain("ETF") == MarketFeedDomain.SECURITIES_MARKET


def test_etf_buy_requires_whole_lots_and_sell_cannot_split_odd_lot():
    rule = SimpleNamespace(
        buy_lot_size=100,
        sell_min_unit=100,
        sell_odd_lot_allowed=True,
    )
    policy = EtfTradingPolicy()

    with pytest.raises(BusinessValidationError) as buy_error:
        policy.validate_buy(request=_request(volume=101), rule=rule)
    assert buy_error.value.error_code == "ETF_BUY_LOT_MISMATCH"

    position = _position(total_volume=150, yesterday_volume=150, available_volume=150)
    with pytest.raises(BusinessValidationError) as sell_error:
        policy.validate_sell(
            request=_request(direction="SELL", volume=125),
            rule=rule,
            position=position,
        )
    assert sell_error.value.error_code == "ETF_SELL_ODD_LOT_SPLIT"

    policy.validate_sell(
        request=_request(direction="SELL", volume=150),
        rule=rule,
        position=position,
    )


@pytest.mark.parametrize(
    ("market_tplus", "available", "locked"),
    [(0, 100, 0), (1, 0, 100)],
)
def test_etf_buy_availability_uses_contract_market_tplus(
    market_tplus, available, locked
):
    position = _position(
        total_volume=0,
        yesterday_volume=0,
        available_volume=0,
        position_cost=Decimal("0"),
        daily_pnl_base_cost=Decimal("0"),
        yesterday_pnl_base_cost=Decimal("0"),
    )
    CashSecurityPositionService.apply_buy(
        position,
        instrument_type="ETF",
        market_tplus=market_tplus,
        volume=100,
        turnover=Decimal("400.1"),
    )
    assert position.available_volume == available
    assert position.settlement_locked_volume == locked


def test_etf_t_plus_one_rejects_selling_today_but_t_zero_allows_it():
    t1 = _position(
        total_volume=100,
        today_volume=100,
        yesterday_volume=0,
        available_volume=0,
        frozen_volume=100,
        settlement_locked_volume=100,
    )
    with pytest.raises(DataAccessError) as error:
        CashSecurityPositionService.apply_sell(
            t1, instrument_type="ETF", market_tplus=1, volume=100
        )
    assert error.value.error_code == "ETF_T_PLUS_ONE_VIOLATION"

    t0 = _position(
        total_volume=100,
        today_volume=100,
        yesterday_volume=0,
        available_volume=0,
        frozen_volume=100,
    )
    CashSecurityPositionService.apply_sell(
        t0, instrument_type="ETF", market_tplus=0, volume=100
    )
    assert t0.total_volume == 0


def test_etf_position_mutation_rejects_missing_tplus_reference():
    with pytest.raises(DataAccessError):
        CashSecurityPositionService.apply_buy(
            _position(),
            instrument_type="ETF",
            volume=100,
            turnover=Decimal("400"),
        )


def test_etf_order_rejects_stale_rule_that_disagrees_with_catalog():
    rule = SimpleNamespace(
        settlement_days=1,
        buy_lot_size=100,
        price_limit_type="RATIO",
    )
    fact = SimpleNamespace(
        is_suspended=False,
        is_tradeable=True,
        upper_limit_price=Decimal("5"),
        lower_limit_price=Decimal("3"),
    )
    policy = EtfTradingPolicy(
        rule_repository=SimpleNamespace(
            resolve_for_trading_day=lambda *args, **kwargs: rule
        ),
        fact_repository=SimpleNamespace(get=lambda *args, **kwargs: fact),
    )
    instrument = SimpleNamespace(
        id=1,
        instrument_type="ETF",
        market_type="FUND",
        is_active=True,
        is_tradeable=True,
        market_tplus=0,
        round_lot=100,
        min_volume=1,
        max_volume=1_000_000,
        price_tick=Decimal("0.001"),
    )

    with pytest.raises(BusinessRuleError) as error:
        policy.resolve_and_validate(
            SimpleNamespace(),
            instrument=instrument,
            request=_request(),
            trading_day=date(2026, 9, 3),
        )
    assert error.value.error_code == "ETF_TRADING_RULE_REFERENCE_MISMATCH"
