from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.common.exceptions import DataAccessError
from app.matching.cash_security import (
    CashSecurityMarketSnapshot,
    CashSecurityMatchingStrategy,
    CashSecurityOrderSnapshot,
)
from app.services.cash_security_position_service import CashSecurityPositionService
from app.services.cash_security_settlement_service import (
    CashSecuritySettlementService,
)


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


def test_cash_position_daily_basis_tracks_buys_and_partial_sells():
    position = _position(
        total_volume=100,
        today_volume=0,
        yesterday_volume=100,
        frozen_volume=0,
        settlement_locked_volume=0,
        available_volume=100,
        position_cost=Decimal("1000"),
        daily_pnl_base_cost=Decimal("1200"),
    )

    CashSecurityPositionService.apply_buy(
        position,
        instrument_type="CONVERTIBLE_BOND",
        volume=10,
        turnover=Decimal("130"),
    )
    assert position.daily_pnl_base_cost == Decimal("1330.000000")

    position.frozen_volume = 55
    CashSecurityPositionService.apply_sell(
        position,
        instrument_type="CONVERTIBLE_BOND",
        volume=55,
    )

    # The sold convertible bonds consume the yesterday bucket first; the
    # same-day buy basis stays intact.  This must not use aggregate pro rata.
    assert position.total_volume == 55
    assert position.daily_pnl_base_cost == Decimal("670.000000")


def test_unestablished_historical_basis_survives_first_buy():
    position = _position(
        total_volume=100, today_volume=0, yesterday_volume=100,
        frozen_volume=0, available_volume=100,
        daily_pnl_base_cost=Decimal("1200"),
        yesterday_pnl_base_cost=Decimal("0"), today_pnl_base_cost=Decimal("0"),
        daily_pnl_base_established=False,
    )

    CashSecurityPositionService.apply_buy(
        position, instrument_type="STOCK", volume=10, turnover=Decimal("130")
    )

    assert position.daily_pnl_base_established is False
    assert position.yesterday_pnl_base_cost == Decimal("0")
    assert position.today_pnl_base_cost == Decimal("130.000000")
    assert position.daily_pnl_base_cost == Decimal("1330.000000")


def test_unestablished_historical_stock_sell_does_not_guess_yesterday_basis():
    position = _position(
        total_volume=100, today_volume=0, yesterday_volume=100,
        frozen_volume=20, settlement_locked_volume=0, available_volume=80,
        daily_pnl_base_cost=Decimal("1200"),
        yesterday_pnl_base_cost=Decimal("0"), today_pnl_base_cost=Decimal("0"),
        daily_pnl_base_established=False,
    )

    CashSecurityPositionService.apply_sell(
        position, instrument_type="STOCK", volume=20
    )

    assert position.daily_pnl_base_established is False
    assert position.daily_pnl_base_cost == Decimal("1200.000000")


def test_unestablished_convertible_sell_reduces_only_known_today_bucket():
    position = _position(
        total_volume=110, today_volume=10, yesterday_volume=100,
        frozen_volume=105, settlement_locked_volume=0, available_volume=5,
        position_cost=Decimal("1130"), daily_pnl_base_cost=Decimal("1330"),
        yesterday_pnl_base_cost=Decimal("0"), today_pnl_base_cost=Decimal("130"),
        daily_pnl_base_established=False,
    )

    CashSecurityPositionService.apply_sell(
        position, instrument_type="CONVERTIBLE_BOND", volume=105
    )

    assert position.yesterday_volume == 0
    assert position.today_volume == 5
    assert position.today_pnl_base_cost == Decimal("65.000000")
    assert position.daily_pnl_base_cost == Decimal("1265.000000")


def test_established_stock_sell_reduces_only_yesterday_bucket():
    position = _position(
        total_volume=100, today_volume=0, yesterday_volume=100,
        frozen_volume=20, settlement_locked_volume=0, available_volume=80,
        daily_pnl_base_cost=Decimal("1200"),
        yesterday_pnl_base_cost=Decimal("1200"), today_pnl_base_cost=Decimal("0"),
        daily_pnl_base_established=True,
    )

    CashSecurityPositionService.apply_sell(
        position, instrument_type="STOCK", volume=20
    )

    assert position.yesterday_pnl_base_cost == Decimal("960.000000")
    assert position.daily_pnl_base_cost == Decimal("960.000000")


@pytest.mark.parametrize(
    ("instrument_type", "daily_close_pnl", "realized_pnl", "expected_daily", "expected_cumulative"),
    [
        ("STOCK", Decimal("0"), Decimal("0"), Decimal("-2"), Decimal("-2")),
        ("STOCK", Decimal("100"), Decimal("100"), Decimal("98"), Decimal("88")),
        ("STOCK", Decimal("-100"), Decimal("-100"), Decimal("-102"), Decimal("-112")),
        ("CONVERTIBLE_BOND", Decimal("100"), Decimal("100"), Decimal("98"), Decimal("88")),
    ],
)
def test_cash_fill_refreshes_daily_and_cumulative_pnl_facts(
    instrument_type,
    daily_close_pnl,
    realized_pnl,
    expected_daily,
    expected_cumulative,
):
    account = SimpleNamespace(
        instrument_type=instrument_type,
        daily_position_pnl=Decimal("0"),
        daily_close_pnl=daily_close_pnl,
        daily_commission=Decimal("2"),
        realized_pnl=realized_pnl,
        unrealized_pnl=Decimal("0"),
        used_commission=Decimal("2") if realized_pnl == 0 else Decimal("12"),
        daily_pnl=Decimal("0"),
        cumulative_net_pnl=Decimal("0"),
    )

    CashSecuritySettlementService._refresh_account_pnl_facts(account)

    assert account.daily_pnl == expected_daily
    assert account.cumulative_net_pnl == expected_cumulative


def _settlement_position(**overrides):
    values = dict(
        total_volume=0,
        today_volume=0,
        yesterday_volume=0,
        frozen_volume=0,
        settlement_locked_volume=0,
        available_volume=0,
        position_cost=Decimal("0"),
        average_open_price=Decimal("0"),
        realized_pnl=Decimal("0"),
        daily_close_pnl=Decimal("0"),
        updated_at=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _settlement_service(*, order, account, position, fee=Decimal("2")):
    order_repository = Mock()
    order_repository.get_by_order_id_for_update.return_value = order
    account_repository = Mock()
    account_repository.get_by_account_id_for_update.return_value = account
    instrument_repository = Mock()
    instrument_repository.get.return_value = SimpleNamespace(
        instrument_type=order.instrument_type,
        is_active=True,
        is_tradeable=True,
        contract_multiplier=Decimal("1"),
    )
    service = CashSecuritySettlementService(
        order_repository=order_repository,
        account_repository=account_repository,
        instrument_repository=instrument_repository,
        position_repository=Mock(),
        trade_repository=Mock(get_by_order_market_event=Mock(return_value=None)),
        snapshot_repository=Mock(),
        fee_repository=Mock(),
        outbox_repository=Mock(),
    )
    service._settle_fees = Mock(return_value=fee)
    service._position_for_update = Mock(return_value=position)
    service._create_events = Mock()
    return service


def _account(**overrides):
    values = dict(
        account_type="SECURITIES_CASH",
        available_cash=Decimal("0"),
        frozen_cash=Decimal("0"),
        frozen_commission=Decimal("0"),
        cash_balance=Decimal("0"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        daily_position_pnl=Decimal("0"),
        daily_close_pnl=Decimal("0"),
        used_commission=Decimal("0"),
        daily_commission=Decimal("0"),
        daily_pnl=Decimal("0"),
        cumulative_net_pnl=Decimal("0"),
        updated_at=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _settlement_order(*, direction, instrument_type="STOCK"):
    return SimpleNamespace(
        order_id="O-CASH",
        account_id="A-CASH",
        order_book_id="600519",
        exchange_id="SSE",
        symbol="600519",
        trading_day="2026-08-18",
        instrument_type=instrument_type,
        direction=direction,
        offset_flag=None,
        status="ACCEPTED",
        remaining_volume=100,
        traded_volume=0,
        average_price=Decimal("0"),
        limit_price=Decimal("10"),
        frozen_cash=Decimal("1000") if direction == "BUY" else Decimal("0"),
        frozen_commission=Decimal("2") if direction == "BUY" else Decimal("0"),
        frozen_position_volume=100 if direction == "SELL" else 0,
        updated_at=None,
    )


def test_buy_fill_updates_postgres_pnl_before_account_fact_event():
    order = _settlement_order(direction="BUY")
    account = _account(
        frozen_cash=Decimal("1000"),
        frozen_commission=Decimal("2"),
        cash_balance=Decimal("1000"),
    )
    service = _settlement_service(
        order=order,
        account=account,
        position=_settlement_position(),
    )
    db = Mock()

    service.settle(
        db,
        order_id=order.order_id,
        market_event_id="T-1",
        market_stream_message_id="1-0",
        tick_event_time=SimpleNamespace(),
        match=SimpleNamespace(matched=True, fill_price=Decimal("10"), fill_volume=100),
    )

    assert account.daily_commission == Decimal("2.000000")
    assert account.daily_pnl == Decimal("-2.000000")
    assert account.cumulative_net_pnl == Decimal("-2.000000")
    assert service._create_events.call_args.kwargs["account"] is account


def test_buy_full_fill_releases_estimated_commission_residual():
    # 回归：全部成交后，预计冻结手续费与逐笔实际手续费的舍入差不能
    # 残留在订单和账户冻结字段上，否则日终对账会因“无法解释的冻结资金”
    # 而失败。
    order = _settlement_order(direction="BUY")
    order.frozen_commission = Decimal("5.022820")
    account = _account(
        frozen_cash=Decimal("1000"),
        frozen_commission=Decimal("5.022820"),
        cash_balance=Decimal("1000"),
    )
    service = _settlement_service(
        order=order,
        account=account,
        position=_settlement_position(),
        fee=Decimal("5.022756"),
    )

    result = service.settle(
        Mock(),
        order_id=order.order_id,
        market_event_id="T-FULL",
        market_stream_message_id="1-0",
        tick_event_time=SimpleNamespace(),
        match=SimpleNamespace(matched=True, fill_price=Decimal("10"), fill_volume=100),
    )

    assert result.action == "SETTLED"
    assert order.frozen_commission == Decimal("0")
    assert order.frozen_cash == Decimal("0")
    assert account.frozen_commission == Decimal("0")
    assert account.frozen_cash == Decimal("0")


@pytest.mark.parametrize(
    ("price", "expected_realized", "expected_daily", "expected_cumulative"),
    [
        (Decimal("10"), Decimal("100"), Decimal("98"), Decimal("98")),
        (Decimal("8"), Decimal("-100"), Decimal("-102"), Decimal("-102")),
    ],
)
def test_stock_sell_fill_keeps_gross_realized_pnl_and_deducts_fee_once(
    price,
    expected_realized,
    expected_daily,
    expected_cumulative,
):
    order = _settlement_order(direction="SELL")
    account = _account()
    position = _settlement_position(
        total_volume=100,
        yesterday_volume=100,
        frozen_volume=100,
        position_cost=Decimal("900"),
    )
    service = _settlement_service(order=order, account=account, position=position)

    service.settle(
        Mock(),
        order_id=order.order_id,
        market_event_id="T-SELL",
        market_stream_message_id="1-0",
        tick_event_time=SimpleNamespace(),
        match=SimpleNamespace(matched=True, fill_price=price, fill_volume=100),
    )

    assert account.realized_pnl == expected_realized.quantize(Decimal("0.000001"))
    assert account.daily_close_pnl == expected_realized.quantize(Decimal("0.000001"))
    assert account.daily_pnl == expected_daily.quantize(Decimal("0.000001"))
    assert account.cumulative_net_pnl == expected_cumulative.quantize(Decimal("0.000001"))


def test_trade_row_flushes_before_fee_components_are_added():
    # 回归：费用明细外键指向 trade.trade_id，且费用行先于 Trade 对象加入
    # Session。若 trade 行未先落库，任何一次 flush（新建持仓或提交）都会
    # 触发 PostgreSQL 外键 cash_security_trade_fee_component_trade_id_fkey。
    order = _settlement_order(direction="BUY")
    account = _account(
        frozen_cash=Decimal("1000"),
        frozen_commission=Decimal("2"),
        cash_balance=Decimal("1000"),
    )
    snapshot = SimpleNamespace(
        fee_type="BROKER_COMMISSION",
        aggregation_scope="TRADE",
        calculation_type="BY_AMOUNT",
        commission_parameter=Decimal("0.001"),
        minimum_fee=Decimal("0"),
    )
    parent = Mock()
    db = parent.db
    service = _settlement_service(
        order=order,
        account=account,
        position=_settlement_position(),
    )
    # 用真实 _settle_fees 验证费用行确实进入 Session，而不是整体 Mock 掉。
    del service._settle_fees
    service.snapshot_repository.list_by_order_ids = Mock(
        return_value={order.order_id: [snapshot]}
    )
    # trade 与 fee 仓储挂到同一父 Mock，保证 mock_calls 是全局调用顺序。
    service.trade_repository = parent.trade_repo
    service.trade_repository.get_by_order_market_event = Mock(return_value=None)
    service.fee_repository = parent.fee_repo

    service.settle(
        db,
        order_id=order.order_id,
        market_event_id="T-FK",
        market_stream_message_id="1-0",
        tick_event_time=SimpleNamespace(),
        match=SimpleNamespace(matched=True, fill_price=Decimal("10"), fill_volume=100),
    )

    names = [entry[0] for entry in parent.mock_calls]
    assert names.index("trade_repo.add") < names.index("db.flush")
    assert names.index("db.flush") < names.index("fee_repo.add_trade_components")
    assert names.index("fee_repo.add_trade_components") < names.index("db.commit")

    # 先以占位值落库，方向分支结算后在同一事务内更新为最终值。
    trade = service.trade_repository.add.call_args.args[1]
    assert trade.commission == Decimal("1.000000")
    assert trade.realized_pnl == Decimal("0")
    assert trade.daily_close_pnl == Decimal("0")
