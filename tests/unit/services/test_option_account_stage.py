from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.common.exceptions import BusinessRuleError
from app.enums.option_enums import (
    InstrumentType,
    MarginPriceMode,
    OptionMarginAlgorithm,
    OptionType,
)
from app.enums.order_enums import OffsetFlag, OrderDirection
from app.services.account_valuation_calculator import (
    AccountValuationCalculator,
)
from app.services.commodity_option_margin_calculator import (
    CommodityFuturesOptionMarginCalculator,
)
from app.services.option_margin_calculator import (
    OptionMarginInput,
    OptionMarginRuleSnapshot,
)
from app.services.option_margin_calculator_resolver import (
    OptionMarginCalculatorResolver,
)
from app.services.option_premium_calculator import OptionPremiumCalculator
from app.services.option_trading_permission_service import (
    OptionTradingPermissionService,
)
from app.services.rule_query_service import RuleQueryService


NOW = datetime(2026, 7, 30, tzinfo=timezone.utc)


def make_rule() -> OptionMarginRuleSnapshot:
    return OptionMarginRuleSnapshot(
        rule_id=1,
        rule_version="V1",
        margin_algorithm=(
            OptionMarginAlgorithm.COMMODITY_FUTURES_OPTION.value
        ),
        margin_adjustment_rate=Decimal("0.12"),
        minimum_guarantee_rate=Decimal("0.07"),
        out_of_money_deduction_rate=Decimal("1"),
        minimum_underlying_margin_ratio=Decimal("0.5"),
        extra_margin_rate=Decimal("0"),
    )


def make_margin_input(option_type: OptionType) -> OptionMarginInput:
    return OptionMarginInput(
        option_type=option_type,
        strike_price=Decimal("8000"),
        option_price=Decimal("100"),
        underlying_price=Decimal("7800"),
        option_multiplier=Decimal("15"),
        underlying_multiplier=Decimal("15"),
        volume=2,
        price_mode=MarginPriceMode.ORDER_FREEZE,
        calculated_at=NOW,
        rule=make_rule(),
        underlying_margin_per_lot=Decimal("14040"),
    )


def test_option_premium_and_account_valuation_use_decimal():
    premium = OptionPremiumCalculator.calculate(
        price=Decimal("100"),
        volume=2,
        multiplier=Decimal("15"),
    )
    assert premium == Decimal("3000.000000")

    result = AccountValuationCalculator.calculate(
        cash_balance=Decimal("97000"),
        futures_unrealized_pnl=Decimal("500"),
        long_option_market_value=Decimal("3300"),
        short_option_market_value=Decimal("1000"),
        used_margin=Decimal("12000"),
        option_used_margin=Decimal("2000"),
        option_realtime_required_margin=Decimal("2500"),
        frozen_margin=Decimal("100"),
        frozen_cash=Decimal("200"),
        frozen_commission=Decimal("10"),
    )
    assert result.net_option_market_value == Decimal("2300.000000")
    assert result.equity == Decimal("99800.000000")
    assert result.effective_required_margin == Decimal("12500.000000")
    assert result.risk_available_cash == Decimal("83690.000000")


@pytest.mark.parametrize(
    ("option_type", "expected_otm"),
    [
        (OptionType.CALL, Decimal("3000.000000")),
        (OptionType.PUT, Decimal("0.000000")),
    ],
)
def test_commodity_option_margin_call_put_otm(
    option_type,
    expected_otm,
):
    result = CommodityFuturesOptionMarginCalculator().calculate(
        make_margin_input(option_type)
    )
    assert result.out_of_money_amount == expected_otm
    assert result.price_mode == MarginPriceMode.ORDER_FREEZE
    assert result.total_margin >= Decimal("0")


@pytest.mark.parametrize(
    ("option_type", "risk", "per_lot", "total"),
    [
        (
            OptionType.CALL,
            Decimal("11040.000000"),
            Decimal("12540.000000"),
            Decimal("25080.000000"),
        ),
        (
            OptionType.PUT,
            Decimal("14040.000000"),
            Decimal("15540.000000"),
            Decimal("31080.000000"),
        ),
    ],
)
def test_commodity_option_margin_exact_decimal_result(
    option_type,
    risk,
    per_lot,
    total,
):
    """逐项核对权利金、虚值扣减、最低保障和总保证金，禁止近似比较。"""

    result = CommodityFuturesOptionMarginCalculator().calculate(
        make_margin_input(option_type)
    )

    assert result.premium_component == Decimal("1500.000000")
    assert result.risk_component == risk
    assert result.margin_per_lot == per_lot
    assert result.total_margin == total


def test_long_and_short_option_equity_matches_cash_plus_market_value():
    """权利金现金流与期权市值必须相互抵消，只留下真实盯市盈亏。"""

    long_result = AccountValuationCalculator.calculate(
        cash_balance=Decimal("97000"),
        futures_unrealized_pnl=Decimal("0"),
        long_option_market_value=Decimal("3300"),
        short_option_market_value=Decimal("0"),
        used_margin=Decimal("0"),
        option_used_margin=Decimal("0"),
        option_realtime_required_margin=Decimal("0"),
        frozen_margin=Decimal("0"),
        frozen_cash=Decimal("0"),
        frozen_commission=Decimal("0"),
    )
    short_result = AccountValuationCalculator.calculate(
        cash_balance=Decimal("103000"),
        futures_unrealized_pnl=Decimal("0"),
        long_option_market_value=Decimal("0"),
        short_option_market_value=Decimal("3300"),
        used_margin=Decimal("2000"),
        option_used_margin=Decimal("2000"),
        option_realtime_required_margin=Decimal("2500"),
        frozen_margin=Decimal("0"),
        frozen_cash=Decimal("0"),
        frozen_commission=Decimal("0"),
    )

    # 多头成本3000、现值3300，权益增加300。
    assert long_result.equity == Decimal("100300.000000")
    # 空头收取3000、回补负债3300，权益减少300。
    assert short_result.equity == Decimal("99700.000000")
    assert short_result.effective_required_margin == Decimal("2500.000000")
    assert short_result.risk_available_cash == Decimal("97200.000000")


def test_margin_resolver_rejects_unknown_algorithm():
    with pytest.raises(BusinessRuleError) as exc_info:
        OptionMarginCalculatorResolver().resolve(
            instrument_type=InstrumentType.FUTURES_OPTION.value,
            exchange_id="SHFE",
            margin_algorithm="UNKNOWN",
        )
    assert exc_info.value.error_code == "OPTION_MARGIN_ALGORITHM_NOT_FOUND"


def make_permission_config(**overrides):
    values = {
        "option_trading_enabled": True,
        "commodity_option_trading_enabled": True,
        "index_option_buy_trading_enabled": True,
        "index_option_short_trading_enabled": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_account(**overrides):
    values = {
        "account_type": "FUTURES",
        "option_trading_enabled": True,
        "risk_state": "NORMAL",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_index_option_sell_open_is_always_rejected():
    service = OptionTradingPermissionService(make_permission_config())
    with pytest.raises(BusinessRuleError) as exc_info:
        service.validate(
            account=make_account(),
            instrument=SimpleNamespace(
                instrument_type=InstrumentType.INDEX_OPTION.value
            ),
            direction=OrderDirection.SELL,
            offset_flag=OffsetFlag.OPEN,
        )
    assert (
        exc_info.value.error_code
        == "INDEX_OPTION_SHORT_TRADING_UNAVAILABLE"
    )


def test_margin_deficit_blocks_open_but_allows_close():
    service = OptionTradingPermissionService(make_permission_config())
    instrument = SimpleNamespace(
        instrument_type=InstrumentType.FUTURES_OPTION.value
    )
    account = make_account(risk_state="MARGIN_DEFICIT")
    with pytest.raises(BusinessRuleError) as exc_info:
        service.validate(
            account=account,
            instrument=instrument,
            direction=OrderDirection.BUY,
            offset_flag=OffsetFlag.OPEN,
        )
    assert exc_info.value.error_code == "ACCOUNT_RISK_INCREASE_BLOCKED"

    service.validate(
        account=account,
        instrument=instrument,
        direction=OrderDirection.BUY,
        offset_flag=OffsetFlag.CLOSE,
    )


def test_index_option_sell_open_returns_fixed_error_before_rule_lookup():
    instrument_repository = Mock()
    instrument_repository.get.return_value = SimpleNamespace(
        id=10,
        instrument_type=InstrumentType.INDEX_OPTION.value,
        is_active=True,
    )
    fee_items = Mock()
    service = RuleQueryService(
        instrument_repository=instrument_repository,
        margin_repository=Mock(),
        fee_repository=Mock(),
        fee_item_repository=fee_items,
    )

    with pytest.raises(BusinessRuleError) as exc_info:
        service.get_order_rules(
            db=Mock(),
            exchange_id="CFFEX",
            symbol="IO2609-C-4000",
            trading_day=date(2026, 7, 30),
            direction="SELL",
            offset_flag="OPEN",
        )
    assert (
        exc_info.value.error_code
        == "INDEX_OPTION_SHORT_TRADING_UNAVAILABLE"
    )
    fee_items.resolve.assert_not_called()
