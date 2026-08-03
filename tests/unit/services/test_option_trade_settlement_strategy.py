from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services.fee_calculator import FeeCalculator
from app.services.option_trade_settlement_strategy import (
    OptionTradeSettlementStrategy,
)


NOW = datetime(2026, 7, 30, tzinfo=timezone.utc)


def make_account(**overrides):
    values = {
        "frozen_margin": Decimal("0"),
        "frozen_cash": Decimal("0"),
        "frozen_commission": Decimal("2"),
        "used_margin": Decimal("0"),
        "option_used_margin": Decimal("0"),
        "used_commission": Decimal("0"),
        "daily_commission": Decimal("0"),
        "cash_balance": Decimal("100000"),
        "long_option_market_value": Decimal("0"),
        "short_option_market_value": Decimal("0"),
        "unrealized_pnl": Decimal("0"),
        "option_realtime_required_margin": Decimal("0"),
        "daily_position_pnl": Decimal("0"),
        "daily_close_pnl": Decimal("0"),
        "daily_pnl": Decimal("0"),
        "risk_state": "NORMAL",
        "updated_at": NOW,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_order(direction: str, **overrides):
    values = {
        "order_id": "O1",
        "account_id": "A1",
        "order_book_id": "AG2609-C-8000",
        "exchange_id": "SHFE",
        "symbol": "AG2609-C-8000",
        "trading_day": date(2026, 7, 30),
        "instrument_type": "FUTURES_OPTION",
        "direction": direction,
        "offset_flag": "OPEN",
        "frozen_margin": Decimal("0"),
        "frozen_cash": Decimal("3000") if direction == "BUY" else Decimal("0"),
        "frozen_commission": Decimal("2"),
        "commission_type": "BY_VOLUME",
        "commission_parameter": Decimal("1"),
        "commission_contract_multiplier": Decimal("15"),
        "margin_rule_id": None,
        "margin_rule_version": None,
        "margin_calculation_version": None,
        "margin_rule_snapshot": None,
        "margin_price_mode": None,
        "margin_underlying_price": None,
        "traded_volume": 0,
        "remaining_volume": 2,
        "average_price": None,
        "status": "ACCEPTED",
        "updated_at": NOW,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    ("direction", "cash_delta", "market_field"),
    [
        ("BUY", Decimal("-1501"), "long_option_market_value"),
        ("SELL", Decimal("1499"), "short_option_market_value"),
    ],
)
def test_option_open_settlement_uses_premium_cash_flow(
    direction,
    cash_delta,
    market_field,
):
    order = make_order(direction)
    account = make_account(
        frozen_cash=order.frozen_cash,
    )
    trade_repository = Mock()
    position_repository = Mock()
    # 参考数据已变化，历史订单仍必须使用接单时保存的15倍快照。
    instrument = SimpleNamespace(contract_multiplier=Decimal("99"))
    command = SimpleNamespace(
        market_event_id="M1",
        market_stream_message_id="1-0",
        tick_event_time=NOW,
    )

    trade = OptionTradeSettlementStrategy().apply_open(
        db=Mock(),
        order=order,
        account=account,
        instrument=instrument,
        command=command,
        fill_volume=1,
        fill_price=Decimal("100"),
        remaining_before=2,
        traded_before=0,
        average_before=Decimal("0"),
        position=None,
        trade_id="T1",
        position_id="P1",
        position_detail_id="PD1",
        now=NOW,
        fee_calculator=FeeCalculator(),
        trade_repository=trade_repository,
        position_repository=position_repository,
    )

    assert trade.premium_cash_flow == (
        Decimal("-1500.000000")
        if direction == "BUY"
        else Decimal("1500.000000")
    )
    assert account.cash_balance == Decimal("100000") + cash_delta
    assert getattr(account, market_field) == Decimal("1500.000000")
    assert order.remaining_volume == 1
    assert order.frozen_commission == Decimal("1.000000")
    if direction == "BUY":
        assert order.frozen_cash == Decimal("1500.000000")
    trade_repository.add.assert_called_once()
    assert position_repository.add.call_count == 1
    position_repository.add_detail.assert_called_once()


def test_option_buy_partial_fills_conserve_cash_and_frozen_resources():
    """两次不同价格成交后，权利金、手续费、冻结资金和权益必须严格守恒。"""

    order = make_order("BUY")
    account = make_account(
        frozen_cash=order.frozen_cash,
        frozen_commission=order.frozen_commission,
    )
    trade_repository = Mock()
    position_repository = Mock()
    instrument = SimpleNamespace(contract_multiplier=Decimal("15"))
    command = SimpleNamespace(
        market_event_id="M1",
        market_stream_message_id="1-0",
        tick_event_time=NOW,
    )
    strategy = OptionTradeSettlementStrategy()

    strategy.apply_open(
        db=Mock(),
        order=order,
        account=account,
        instrument=instrument,
        command=command,
        fill_volume=1,
        fill_price=Decimal("100"),
        remaining_before=2,
        traded_before=0,
        average_before=Decimal("0"),
        position=None,
        trade_id="T1",
        position_id="P1",
        position_detail_id="PD1",
        now=NOW,
        fee_calculator=FeeCalculator(),
        trade_repository=trade_repository,
        position_repository=position_repository,
    )
    position = position_repository.add.call_args.args[1]
    strategy.apply_open(
        db=Mock(),
        order=order,
        account=account,
        instrument=instrument,
        command=command,
        fill_volume=1,
        fill_price=Decimal("110"),
        remaining_before=1,
        traded_before=1,
        average_before=Decimal("100"),
        position=position,
        trade_id="T2",
        position_id="P1",
        position_detail_id="PD2",
        now=NOW,
        fee_calculator=FeeCalculator(),
        trade_repository=trade_repository,
        position_repository=position_repository,
    )

    assert order.status == "FILLED"
    assert order.remaining_volume == 0
    assert order.average_price == Decimal("105.000000")
    assert order.frozen_cash == Decimal("0.000000")
    assert order.frozen_commission == Decimal("0.000000")
    assert account.frozen_cash == Decimal("0.000000")
    assert account.frozen_commission == Decimal("0.000000")
    # 权利金=100*15+110*15=3150，手续费=2。
    assert account.cash_balance == Decimal("96848.000000")
    assert account.long_option_market_value == Decimal("3150.000000")
    assert account.equity == Decimal("99998.000000")
    assert account.used_commission == Decimal("2.000000")
    assert account.daily_pnl == Decimal("-2.000000")
    assert position.total_volume == 2
    assert position.position_cost == Decimal("3150.000000")
    assert position_repository.add_detail.call_count == 2
    assert trade_repository.add.call_count == 2


def test_option_sell_open_immediately_sets_realtime_margin_snapshots():
    """卖出开仓后无需等待下一条行情，账户和持仓即可立即安全平仓。"""

    order = make_order(
        "SELL",
        frozen_margin=Decimal("12000"),
    )
    account = make_account(
        frozen_margin=Decimal("12000"),
    )
    trade_repository = Mock()
    position_repository = Mock()

    OptionTradeSettlementStrategy().apply_open(
        db=Mock(),
        order=order,
        account=account,
        instrument=SimpleNamespace(contract_multiplier=Decimal("15")),
        command=SimpleNamespace(
            market_event_id="M-SELL",
            market_stream_message_id="1-0",
            tick_event_time=NOW,
        ),
        fill_volume=1,
        fill_price=Decimal("100"),
        remaining_before=2,
        traded_before=0,
        average_before=Decimal("0"),
        position=None,
        trade_id="T-SELL",
        position_id="P-SELL",
        position_detail_id="PD-SELL",
        now=NOW,
        fee_calculator=FeeCalculator(),
        trade_repository=trade_repository,
        position_repository=position_repository,
    )

    position = position_repository.add.call_args.args[1]
    detail = position_repository.add_detail.call_args.args[1]
    assert account.option_used_margin == Decimal("6000.000000")
    assert account.option_realtime_required_margin == Decimal(
        "6000.000000"
    )
    assert position.realtime_required_margin == Decimal("6000.000000")
    assert detail.realtime_required_margin == Decimal("6000.000000")
