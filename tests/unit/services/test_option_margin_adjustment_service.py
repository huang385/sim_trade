from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.common.exceptions import DataAccessError
from app.infrastructure.market_data.market_tick_store import MarketTickStore
from app.services.option_margin_adjustment_service import (
    OptionMarginAdjustmentService,
)


NOW = datetime(2026, 7, 30, 10, 0, tzinfo=timezone.utc)


def market_values(symbol: str, price: str) -> dict[str, str]:
    return MarketTickStore.tick_to_mapping(
        SimpleNamespace(
            model_dump=lambda **_kwargs: {
                "source_event_id": f"TICK-{symbol}",
                "source": "YMM_LIVE_DATA",
                "ingest_type": "LIVE_CALLBACK",
                "order_book_id": symbol,
                "exchange_id": "SHFE",
                "symbol": symbol,
                "trading_day": date(2026, 7, 30),
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


def make_account(**overrides):
    values = {
        "account_id": "A1",
        "cash_balance": Decimal("10000"),
        "available_cash": Decimal("9000"),
        "risk_available_cash": Decimal("0"),  # 故意放置过期快照
        "equity": Decimal("10000"),
        "used_margin": Decimal("1000"),
        "option_used_margin": Decimal("1000"),
        "option_realtime_required_margin": Decimal("1000"),
        "long_option_market_value": Decimal("0"),
        "short_option_market_value": Decimal("0"),
        "net_option_market_value": Decimal("0"),
        "unrealized_pnl": Decimal("0"),
        "frozen_margin": Decimal("0"),
        "frozen_cash": Decimal("0"),
        "frozen_commission": Decimal("0"),
        "risk_state": "NORMAL",
        "updated_at": NOW,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_position(**overrides):
    values = {
        "position_id": "P1",
        "account_id": "A1",
        "order_book_id": "AGOPT",
        "exchange_id": "SHFE",
        "symbol": "AGOPT",
        "instrument_type": "FUTURES_OPTION",
        "direction": "SHORT",
        "total_volume": 2,
        "used_margin": Decimal("1000"),
        "realtime_required_margin": Decimal("1000"),
        "multiplier_snapshot": Decimal("10"),
        "margin_rule_id": 1,
        "margin_rule_version": "V1",
        "margin_rule_snapshot": {
            "rule_id": "1",
            "rule_version": "V1",
            "margin_algorithm": "COMMODITY_FUTURES_OPTION",
            "margin_adjustment_rate": "0.10",
            "minimum_guarantee_rate": "0.05",
            "out_of_money_deduction_rate": "1",
            "minimum_underlying_margin_ratio": "0.5",
            "extra_margin_rate": "0",
            "underlying_margin_rate": "0.1",
            "underlying_multiplier": "10",
        },
        "margin_price_mode": "ORDER_FREEZE",
        "margin_underlying_price": Decimal("100"),
        "margin_option_price": Decimal("50"),
        "margin_calculated_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_details():
    return [
        SimpleNamespace(
            remaining_volume=1,
            remaining_margin=Decimal("500"),
            realtime_required_margin=Decimal("500"),
            multiplier_snapshot=Decimal("10"),
        ),
        SimpleNamespace(
            remaining_volume=1,
            remaining_margin=Decimal("500"),
            realtime_required_margin=Decimal("500"),
            multiplier_snapshot=Decimal("10"),
        ),
    ]


def build_service(*, account=None, position=None, details=None, latest=None):
    account = account or make_account()
    position = position or make_position()
    details = details if details is not None else make_details()
    for detail in details:
        if not hasattr(detail, "multiplier_snapshot"):
            detail.multiplier_snapshot = position.multiplier_snapshot
        if not hasattr(detail, "margin_rule_id"):
            detail.margin_rule_id = position.margin_rule_id
        if not hasattr(detail, "margin_rule_version"):
            detail.margin_rule_version = position.margin_rule_version
        if not hasattr(detail, "margin_rule_snapshot"):
            detail.margin_rule_snapshot = position.margin_rule_snapshot
    option = SimpleNamespace(
        id=1,
        order_book_id="AGOPT",
        exchange_id="SHFE",
        symbol="AGOPT",
        instrument_type="FUTURES_OPTION",
        underlying_instrument_id=2,
        option_type="CALL",
        strike_price=Decimal("110"),
        # 当前参考数据乘数故意与不可变持仓快照不同。
        contract_multiplier=Decimal("99"),
    )
    underlying = SimpleNamespace(
        id=2,
        order_book_id="AG",
        exchange_id="SHFE",
        symbol="AG",
    )
    account_repository = Mock()
    account_repository.get_by_account_id_for_update.return_value = account
    position_repository = Mock()
    position_repository.get_by_position_id_for_update.return_value = position
    position_repository.list_open_details_for_update.return_value = details
    instrument_repository = Mock()
    instrument_repository.get_by_order_book_id.return_value = option
    instrument_repository.get_by_id.return_value = underlying
    market_store = Mock()
    market_store.get_latest_many.return_value = latest or {
        ("SHFE", "AGOPT"): market_values("AGOPT", "100"),
        ("SHFE", "AG"): market_values("AG", "100"),
    }
    return (
        OptionMarginAdjustmentService(
            market_tick_store=market_store,
            account_repository=account_repository,
            position_repository=position_repository,
            instrument_repository=instrument_repository,
        ),
        account,
        position,
        details,
    )


def test_adjust_recalculates_risk_instead_of_using_stale_risk_cash():
    service, account, position, details = build_service()
    db = Mock()

    service.adjust(db, account_id="A1", position_id="P1")

    # 每手：权利金1000 + max(标的保证金100-虚值100, 最低50) = 1050。
    assert position.realtime_required_margin == Decimal("2100.000000")
    assert position.used_margin == Decimal("2100.000000")
    assert account.used_margin == Decimal("2100.000000")
    assert account.option_used_margin == Decimal("2100.000000")
    assert account.available_cash == Decimal("7900.000000")
    assert account.risk_available_cash == Decimal("7900.000000")
    assert account.risk_state == "NORMAL"
    assert [item.remaining_margin for item in details] == [
        Decimal("1050.000000"),
        Decimal("1050.000000"),
    ]
    db.commit.assert_called_once_with()
    db.rollback.assert_not_called()


def test_index_option_short_margin_uses_cffex_formula_and_releases_downward():
    """股指期权复用同一调整事务，仅由解析器切换保证金公式。"""

    snapshot = {
        "rule_id": "2",
        "rule_version": "CFFEX-V1",
        "margin_algorithm": "CFFEX_INDEX_OPTION",
        "margin_adjustment_rate": "0.12",
        "minimum_guarantee_rate": "0.07",
        "out_of_money_deduction_rate": "1",
        "minimum_underlying_margin_ratio": "0",
        "extra_margin_rate": "0",
    }
    account = make_account(
        cash_balance=Decimal("109800"),
        available_cash=Decimal("41800"),
        risk_available_cash=Decimal("41800"),
        equity=Decimal("99800"),
        used_margin=Decimal("58000"),
        option_used_margin=Decimal("58000"),
        option_realtime_required_margin=Decimal("58000"),
        short_option_market_value=Decimal("10000"),
    )
    position = make_position(
        order_book_id="IO2609-C-4000",
        exchange_id="CFFEX",
        symbol="IO2609-C-4000",
        instrument_type="INDEX_OPTION",
        total_volume=1,
        used_margin=Decimal("58000"),
        realtime_required_margin=Decimal("58000"),
        multiplier_snapshot=Decimal("100"),
        margin_rule_id=2,
        margin_rule_version="CFFEX-V1",
        margin_rule_snapshot=snapshot,
    )
    details = [
        SimpleNamespace(
            remaining_volume=1,
            remaining_margin=Decimal("58000"),
            realtime_required_margin=Decimal("58000"),
            multiplier_snapshot=Decimal("100"),
            margin_rule_id=2,
            margin_rule_version="CFFEX-V1",
            margin_rule_snapshot=snapshot,
        )
    ]
    latest = {
        ("CFFEX", "IO2609-C-4000"): market_values(
            "IO2609-C-4000", "90"
        ),
        ("CFFEX", "000300"): market_values("000300", "3900"),
    }
    service, account, position, details = build_service(
        account=account,
        position=position,
        details=details,
        latest=latest,
    )
    service.instrument_repository.get_by_order_book_id.return_value = (
        SimpleNamespace(
            id=11,
            order_book_id="IO2609-C-4000",
            exchange_id="CFFEX",
            symbol="IO2609-C-4000",
            instrument_type="INDEX_OPTION",
            underlying_instrument_id=12,
            option_type="CALL",
            strike_price=Decimal("4000"),
            contract_multiplier=Decimal("100"),
        )
    )
    service.instrument_repository.get_by_id.return_value = SimpleNamespace(
        id=12,
        order_book_id="000300",
        exchange_id="CFFEX",
        symbol="000300",
        contract_multiplier=Decimal("1"),
    )

    service.adjust(Mock(), account_id="A1", position_id="P1")

    # 每手：权利金90*100 + max(3900*100*12%-虚值100*100,
    # 最低保障3900*100*7%*12%) = 45800。
    assert position.realtime_required_margin == Decimal("45800.000000")
    assert position.used_margin == Decimal("45800.000000")
    assert details[0].remaining_margin == Decimal("45800.000000")
    assert account.used_margin == Decimal("45800.000000")
    assert account.option_used_margin == Decimal("45800.000000")


def test_local_position_success_cannot_clear_valuation_unavailable():
    account = make_account(risk_state="VALUATION_UNAVAILABLE")
    service, account, _position, _details = build_service(account=account)

    service.adjust(Mock(), account_id="A1", position_id="P1")

    assert account.risk_state == "VALUATION_UNAVAILABLE"


def test_adjust_releases_margin_immediately_when_requirement_decreases():
    account = make_account(
        used_margin=Decimal("3000"),
        option_used_margin=Decimal("3000"),
        option_realtime_required_margin=Decimal("3000"),
    )
    position = make_position(
        used_margin=Decimal("3000"),
        realtime_required_margin=Decimal("3000"),
    )
    details = [
        SimpleNamespace(
            remaining_volume=1,
            remaining_margin=Decimal("1500"),
            realtime_required_margin=Decimal("1500"),
        ),
        SimpleNamespace(
            remaining_volume=1,
            remaining_margin=Decimal("1500"),
            realtime_required_margin=Decimal("1500"),
        ),
    ]
    service, account, position, details = build_service(
        account=account,
        position=position,
        details=details,
    )
    db = Mock()

    service.adjust(db, account_id="A1", position_id="P1")

    assert position.used_margin == Decimal("2100.000000")
    assert position.realtime_required_margin == Decimal("2100.000000")
    assert account.used_margin == Decimal("2100.000000")
    assert account.option_used_margin == Decimal("2100.000000")
    assert account.available_cash == Decimal("7900.000000")
    assert [item.remaining_margin for item in details] == [
        Decimal("1050.000000"),
        Decimal("1050.000000"),
    ]
    assert [item.realtime_required_margin for item in details] == [
        Decimal("1050.000000"),
        Decimal("1050.000000"),
    ]
    db.commit.assert_called_once_with()
    db.rollback.assert_not_called()


def test_adjust_has_no_release_threshold_even_for_smallest_money_delta():
    account = make_account(
        used_margin=Decimal("2100.000001"),
        option_used_margin=Decimal("2100.000001"),
        option_realtime_required_margin=Decimal("2100.000001"),
    )
    position = make_position(
        used_margin=Decimal("2100.000001"),
        realtime_required_margin=Decimal("2100.000001"),
    )
    details = [
        SimpleNamespace(
            remaining_volume=1,
            remaining_margin=Decimal("1050.0000005"),
            realtime_required_margin=Decimal("1050.0000005"),
        ),
        SimpleNamespace(
            remaining_volume=1,
            remaining_margin=Decimal("1050.0000005"),
            realtime_required_margin=Decimal("1050.0000005"),
        ),
    ]
    service, account, position, details = build_service(
        account=account,
        position=position,
        details=details,
    )

    service.adjust(Mock(), account_id="A1", position_id="P1")

    assert position.used_margin == Decimal("2100.000000")
    assert account.used_margin == Decimal("2100.000000")
    assert sum(
        (item.remaining_margin for item in details),
        Decimal("0"),
    ) == Decimal("2100.000000")


def test_release_is_allowed_even_if_account_remains_in_margin_deficit():
    account = make_account(
        cash_balance=Decimal("1500"),
        used_margin=Decimal("3000"),
        option_used_margin=Decimal("3000"),
        option_realtime_required_margin=Decimal("3000"),
    )
    position = make_position(
        used_margin=Decimal("3000"),
        realtime_required_margin=Decimal("3000"),
    )
    details = [
        SimpleNamespace(
            remaining_volume=1,
            remaining_margin=Decimal("1500"),
            realtime_required_margin=Decimal("1500"),
        ),
        SimpleNamespace(
            remaining_volume=1,
            remaining_margin=Decimal("1500"),
            realtime_required_margin=Decimal("1500"),
        ),
    ]
    service, account, position, _details = build_service(
        account=account,
        position=position,
        details=details,
    )

    service.adjust(Mock(), account_id="A1", position_id="P1")

    assert position.used_margin == Decimal("2100.000000")
    assert account.used_margin == Decimal("2100.000000")
    assert account.option_used_margin == Decimal("2100.000000")
    assert account.risk_available_cash == Decimal("-600.000000")
    assert account.risk_state == "MARGIN_DEFICIT"


def test_adjust_deficit_updates_risk_snapshot_without_booking_margin():
    account = make_account(cash_balance=Decimal("1500"))
    service, account, position, details = build_service(account=account)
    db = Mock()

    service.adjust(db, account_id="A1", position_id="P1")

    assert position.realtime_required_margin == Decimal("2100.000000")
    assert position.used_margin == Decimal("1000")
    assert account.used_margin == Decimal("1000")
    assert account.option_used_margin == Decimal("1000")
    assert account.available_cash == Decimal("500.000000")
    assert account.risk_available_cash == Decimal("-600.000000")
    assert account.risk_state == "MARGIN_DEFICIT"
    assert [item.remaining_margin for item in details] == [
        Decimal("500"),
        Decimal("500"),
    ]
    assert [item.realtime_required_margin for item in details] == [
        Decimal("1050.000000"),
        Decimal("1050.000000"),
    ]
    db.commit.assert_called_once_with()


def test_adjust_rejects_inconsistent_position_details_and_rolls_back():
    service, account, position, _details = build_service(
        details=[
            SimpleNamespace(
                remaining_volume=1,
                remaining_margin=Decimal("500"),
                realtime_required_margin=Decimal("500"),
            )
        ]
    )
    db = Mock()
    original = (
        account.used_margin,
        position.used_margin,
    )

    with pytest.raises(DataAccessError) as exc_info:
        service.adjust(db, account_id="A1", position_id="P1")

    assert exc_info.value.error_code == "OPTION_MARGIN_POSITION_INCONSISTENT"
    assert (account.used_margin, position.used_margin) == original
    db.commit.assert_not_called()
    db.rollback.assert_called_once_with()


def test_adjust_missing_market_data_is_business_data_error():
    service, _account, _position, _details = build_service(latest={})
    service.market_tick_store.get_latest_many.return_value = {}
    db = Mock()

    with pytest.raises(DataAccessError) as exc_info:
        service.adjust(db, account_id="A1", position_id="P1")

    assert exc_info.value.error_code == "OPTION_MARGIN_PRICE_UNAVAILABLE"
    db.commit.assert_not_called()
    db.rollback.assert_called_once_with()
