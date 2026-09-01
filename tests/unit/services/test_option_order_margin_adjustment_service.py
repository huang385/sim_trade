from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.common.exceptions import DataAccessError
from app.enums.option_enums import MarginPriceMode
from app.infrastructure.market_data.market_tick_store import MarketTickStore
from app.services.option_order_margin_adjustment_service import (
    OptionOrderMarginAdjustmentResult,
    OptionOrderMarginAdjustmentService,
)
from app.matching.types import MatchResult
from app.services.trade_settlement_service import (
    SettlementCommand,
    TradeSettlementService,
)


NOW = datetime(2026, 9, 1, 5, 45, tzinfo=timezone.utc)


def market_values(
    *, exchange_id: str, order_book_id: str, symbol: str, price: str
) -> dict[str, str]:
    return MarketTickStore.tick_to_mapping(
        SimpleNamespace(
            model_dump=lambda **_kwargs: {
                "source_event_id": f"TICK-{order_book_id}",
                "source": "YMM_LIVE_DATA",
                "ingest_type": "LIVE_CALLBACK",
                "order_book_id": order_book_id,
                "exchange_id": exchange_id,
                "symbol": symbol,
                "trading_day": date(2026, 9, 1),
                "event_time": NOW,
                "local_recv_time": NOW,
                "server_time": NOW,
                "sequence_id": 1,
                "last_price": Decimal(price),
                "pre_close": None,
                "open_price": None,
                "high_price": None,
                "low_price": None,
                "cumulative_volume": 1,
                "cumulative_turnover": None,
                "open_interest": None,
                "bid_price_1": None,
                "bid_volume_1": 0,
                "ask_price_1": None,
                "ask_volume_1": 0,
                "raw_update_time": None,
                "raw_update_millisec": None,
            }
        )
    )


def make_order(**overrides):
    values = {
        "order_id": "O-OPTION-1",
        "account_id": "A001",
        "order_book_id": "JD2609-C-4000",
        "exchange_id": "DCE",
        "symbol": "JD2609-C-4000",
        "status": "ACCEPTED",
        "remaining_volume": 2,
        "instrument_type": "FUTURES_OPTION",
        "direction": "SELL",
        "offset_flag": "OPEN",
        "order_type": "LIMIT",
        "frozen_margin": Decimal("1000"),
        "margin_risk_state": "NORMAL",
        "margin_price_mode": "ORDER_FREEZE",
        "margin_underlying_price": Decimal("4000"),
        "margin_option_price": Decimal("100"),
        "margin_calculation_version": "OPTION_MARGIN_V1",
        "updated_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_account(**overrides):
    values = {
        "account_id": "A001",
        "risk_state": "NORMAL",
        "available_cash": Decimal("10000"),
        "risk_available_cash": Decimal("9000"),
        "frozen_margin": Decimal("1000"),
        "updated_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def margin_result(total: str):
    return SimpleNamespace(
        total_margin=Decimal(total),
        price_mode=MarginPriceMode.REALTIME,
        underlying_price=Decimal("4100"),
        option_price=Decimal("120"),
        calculation_version="OPTION_MARGIN_V1",
    )


def make_service(total: str = "1500"):
    fact_events = Mock()
    service = OptionOrderMarginAdjustmentService(
        market_tick_store=Mock(),
        realtime_fact_events=fact_events,
    )
    service._calculate_required = Mock(return_value=margin_result(total))
    return service


def prepare_adjust(service, order, account):
    service.order_repository = Mock()
    service.order_repository.get_by_order_id_for_update.return_value = order
    service.account_repository = Mock()
    service.account_repository.get_by_account_id_for_update.return_value = (
        account
    )
    service.instrument_repository = Mock()
    service.instrument_repository.get_by_order_book_id.return_value = (
        SimpleNamespace()
    )


def test_active_sell_open_order_adds_margin_difference():
    order = make_order()
    account = make_account()

    result = make_service().ensure_locked(
        Mock(),
        order=order,
        account=account,
        instrument=SimpleNamespace(),
    )

    assert result.action == "ADDED"
    assert result.added_margin == Decimal("500.000000")
    assert order.frozen_margin == Decimal("1500.000000")
    assert account.frozen_margin == Decimal("1500.000000")
    assert account.available_cash == Decimal("9500.000000")
    assert account.risk_available_cash == Decimal("8500.000000")


def test_required_margin_decrease_does_not_release_active_order_funds():
    order = make_order()
    account = make_account()

    result = make_service("800").ensure_locked(
        Mock(),
        order=order,
        account=account,
        instrument=SimpleNamespace(),
    )

    assert result.action == "SUFFICIENT"
    assert order.frozen_margin == Decimal("1000")
    assert account.frozen_margin == Decimal("1000")
    assert account.available_cash == Decimal("10000")


def test_margin_addition_failure_marks_deficit_without_partial_freeze():
    order = make_order()
    account = make_account(
        available_cash=Decimal("400"),
        risk_available_cash=Decimal("400"),
    )

    result = make_service().ensure_locked(
        Mock(),
        order=order,
        account=account,
        instrument=SimpleNamespace(),
    )

    assert result.action == "MARGIN_DEFICIT"
    assert account.risk_state == "MARGIN_DEFICIT"
    assert order.margin_risk_state == "MARGIN_DEFICIT"
    assert order.frozen_margin == Decimal("1000")
    assert account.frozen_margin == Decimal("1000")
    assert account.available_cash == Decimal("400")


def test_successful_retry_clears_order_source_but_not_account_directly():
    """局部订单恢复后只清自身来源，账户等待完整估值恢复。"""

    order = make_order(
        frozen_margin=Decimal("1500"),
        margin_risk_state="MARGIN_DEFICIT",
    )
    account = make_account(
        risk_state="MARGIN_DEFICIT",
        frozen_margin=Decimal("1500"),
    )

    result = make_service("1500").ensure_locked(
        Mock(),
        order=order,
        account=account,
        instrument=SimpleNamespace(),
    )

    assert result.action == "RECOVERED"
    assert order.margin_risk_state == "NORMAL"
    assert account.risk_state == "MARGIN_DEFICIT"
    assert account.frozen_margin == Decimal("1500")
    assert account.available_cash == Decimal("10000")


def test_missing_market_marks_valuation_unavailable_without_freeze():
    service = make_service()
    service._calculate_required.side_effect = DataAccessError(
        "行情不可用",
        error_code="OPTION_MARGIN_PRICE_UNAVAILABLE",
    )
    order = make_order()
    account = make_account()

    result = service.ensure_locked(
        Mock(),
        order=order,
        account=account,
        instrument=SimpleNamespace(),
    )

    assert result.action == "VALUATION_UNAVAILABLE"
    # 缺失行情成为可从PostgreSQL重建的订单风险来源，不能被无关持仓
    # 的成功估值覆盖；行情恢复后本订单仍可重新进入校验并自愈。
    assert order.margin_risk_state == "VALUATION_UNAVAILABLE"
    assert account.risk_state == "VALUATION_UNAVAILABLE"
    assert order.frozen_margin == Decimal("1000")


def test_index_option_order_revalues_with_zero_index_multiplier():
    """不可交易指数的乘数为0时，股指期权仍按期权乘数重估。"""

    snapshot = {
        "rule_id": 70,
        "rule_version": "EXCHANGE_FORMULA_V1",
        "margin_algorithm": "CFFEX_INDEX_OPTION",
        "margin_adjustment_rate": "0.12",
        "minimum_guarantee_rate": "0.5",
        "out_of_money_deduction_rate": "1",
        "minimum_underlying_margin_ratio": "0",
        "extra_margin_rate": "0",
    }
    order = make_order(
        order_book_id="MO2610C6200",
        exchange_id="CFFEX",
        symbol="MO2610-C-6200",
        instrument_type="INDEX_OPTION",
        remaining_volume=1,
        underlying_order_book_id="000852.XSHG",
        limit_price=Decimal("1399.6"),
        commission_contract_multiplier=Decimal("100"),
        margin_rule_id=70,
        margin_rule_version="EXCHANGE_FORMULA_V1",
        margin_rule_snapshot=snapshot,
    )
    instrument = SimpleNamespace(
        id=1,
        order_book_id=order.order_book_id,
        exchange_id=order.exchange_id,
        instrument_type=order.instrument_type,
        underlying_instrument_id=2,
        option_type="CALL",
        strike_price=Decimal("6200"),
    )
    underlying = SimpleNamespace(
        id=2,
        order_book_id="000852.XSHG",
        exchange_id="XSHG",
        contract_multiplier=Decimal("0"),
    )
    store = Mock()
    store.get_latest_many.return_value = {
        ("CFFEX", "MO2610C6200"): market_values(
            exchange_id="CFFEX",
            order_book_id="MO2610C6200",
            symbol="MO2610-C-6200",
            price="1399.6",
        ),
        ("XSHG", "000852.XSHG"): market_values(
            exchange_id="XSHG",
            order_book_id="000852.XSHG",
            symbol="000852.XSHG",
            price="7714.8181",
        ),
    }
    repository = Mock()
    repository.get_by_order_book_id.return_value = underlying
    service = OptionOrderMarginAdjustmentService(
        market_tick_store=store,
        instrument_repository=repository,
    )

    result = service._calculate_required(
        Mock(), order=order, instrument=instrument
    )

    assert result.total_margin == Decimal("232537.817200")


def test_adjust_adds_account_and_order_facts_in_same_transaction():
    service = make_service("1500")
    order = make_order()
    account = make_account()
    prepare_adjust(service, order, account)
    db = Mock()

    result = service.adjust(db, order_id=order.order_id)

    assert result.action == "ADDED"
    service.realtime_fact_events.create_order_margin_updated.assert_called_once()
    service.realtime_fact_events.create_account_updated.assert_called_once()
    db.commit.assert_called_once_with()
    db.rollback.assert_not_called()


def test_adjust_deficit_emits_order_fact_but_no_success_account_fact():
    service = make_service("1500")
    order = make_order()
    account = make_account(
        available_cash=Decimal("100"),
        risk_available_cash=Decimal("100"),
    )
    prepare_adjust(service, order, account)
    db = Mock()

    result = service.adjust(db, order_id=order.order_id)

    assert result.action == "MARGIN_DEFICIT"
    service.realtime_fact_events.create_order_margin_updated.assert_called_once()
    service.realtime_fact_events.create_account_updated.assert_not_called()
    db.commit.assert_called_once_with()


def test_adjust_outbox_failure_rolls_back_order_and_account_transaction():
    service = make_service("1500")
    order = make_order()
    account = make_account()
    prepare_adjust(service, order, account)
    service.realtime_fact_events.create_order_margin_updated.side_effect = (
        RuntimeError("outbox unavailable")
    )
    db = Mock()

    with pytest.raises(RuntimeError, match="outbox unavailable"):
        service.adjust(db, order_id=order.order_id)

    db.commit.assert_not_called()
    db.rollback.assert_called_once_with()
    service.realtime_fact_events.create_account_updated.assert_not_called()


def test_sufficient_replay_does_not_create_duplicate_order_fact():
    service = make_service("800")
    order = make_order()
    account = make_account()
    prepare_adjust(service, order, account)

    result = service.adjust(Mock(), order_id=order.order_id)

    assert result.action == "SUFFICIENT"
    service.realtime_fact_events.create_order_margin_updated.assert_not_called()
    service.realtime_fact_events.create_account_updated.assert_not_called()


def test_settlement_blocks_trade_when_final_margin_check_fails():
    """500ms重估之外，成交事务仍必须执行最后一次保证金检查。"""

    order = make_order(
        order_book_id="JD2609-C-4000",
        exchange_id="DCE",
        symbol="JD2609-C-4000",
        traded_volume=0,
        average_price=None,
    )
    account = make_account()
    order_repository = Mock()
    order_repository.get_by_order_id_for_update.return_value = order
    account_repository = Mock()
    account_repository.get_by_account_id_for_update.return_value = account
    instrument_repository = Mock()
    instrument_repository.get_by_order_book_id.return_value = SimpleNamespace(
        instrument_type="FUTURES_OPTION"
    )
    trade_repository = Mock()
    trade_repository.get_by_order_market_event.return_value = None
    final_checker = Mock()
    final_checker.ensure_locked.return_value = (
        OptionOrderMarginAdjustmentResult(
            action="MARGIN_DEFICIT",
            order_id=order.order_id,
            required_margin=Decimal("2000"),
            account_id=order.account_id,
        )
    )
    db = Mock()

    result = TradeSettlementService(
        order_repository=order_repository,
        account_repository=account_repository,
        instrument_repository=instrument_repository,
        trade_repository=trade_repository,
        option_order_margin_service=final_checker,
    ).settle(
        db,
        SettlementCommand(
            order_id=order.order_id,
            market_event_id="TICK-1",
            market_stream_message_id="1-0",
            tick_event_time=datetime.now(timezone.utc),
            tick_sequence_id=1,
            match_result=MatchResult(
                matched=True,
                fill_price=Decimal("120"),
                fill_volume=1,
                reason=None,
                engine_name="TEST",
                engine_version="1",
            ),
        ),
    )

    assert result.action == "MARGIN_DEFICIT"
    db.commit.assert_called_once()
    trade_repository.add.assert_not_called()
    assert order.traded_volume == 0
    assert order.remaining_volume == 2
