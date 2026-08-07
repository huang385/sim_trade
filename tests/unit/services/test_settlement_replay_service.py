from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.common.exceptions import DataAccessError
from app.services.settlement_replay_service import SettlementReplayService


DAY = date(2026, 8, 6)


def _trade(
    trade_id: str,
    *,
    trading_day: date,
    offset: str,
    price: str,
    volume: int,
    commission: str = "0",
    instrument_type: str = "FUTURES",
):
    return SimpleNamespace(
        id=int(trade_id.removeprefix("T")),
        trade_id=trade_id,
        order_id=f"O-{trade_id}",
        account_id="A1",
        order_book_id="RB2610",
        exchange_id="SHFE",
        symbol="RB2610",
        trading_day=trading_day,
        instrument_type=instrument_type,
        direction="BUY" if offset == "OPEN" else "SELL",
        offset_flag=offset,
        trade_price=Decimal(price),
        trade_volume=volume,
        commission=Decimal(commission),
        premium_cash_flow=Decimal("0"),
        realized_pnl=Decimal("0"),
        daily_close_pnl=Decimal("0"),
        trade_time=datetime(2026, 8, 6, int(trade_id[-1]), tzinfo=timezone.utc),
    )


def _detail(
    detail_id: str,
    *,
    open_trade_id: str,
    open_day: date,
    price: str,
    volume: int,
):
    return SimpleNamespace(
        id=int(detail_id.removeprefix("D")),
        position_detail_id=detail_id,
        position_id="P1",
        account_id="A1",
        open_trade_id=open_trade_id,
        order_book_id="RB2610",
        exchange_id="SHFE",
        symbol="RB2610",
        direction="LONG",
        open_trading_day=open_day,
        open_price=Decimal(price),
        original_volume=volume,
        multiplier_snapshot=Decimal("1"),
    )


def _allocation(
    allocation_id: int,
    *,
    trade_id: str,
    detail_id: str,
    open_day: date,
    open_price: str,
    close_price: str,
    volume: int,
    offset: str,
    commission: str,
):
    return SimpleNamespace(
        id=allocation_id,
        trade_id=trade_id,
        position_id="P1",
        position_detail_id=detail_id,
        open_trading_day=open_day,
        open_price=Decimal(open_price),
        close_trading_day=DAY,
        close_price=Decimal(close_price),
        close_volume=volume,
        resolved_offset_flag=offset,
        commission=Decimal(commission),
        realized_pnl=Decimal("0"),
    )


def _instrument(*, instrument_type: str = "FUTURES"):
    return SimpleNamespace(
        id=1,
        order_book_id="RB2610",
        exchange_id="SHFE",
        symbol="RB2610",
        instrument_type=instrument_type,
        expire_date=date(2026, 9, 30),
        underlying_instrument_id=None,
        option_type=None,
        strike_price=None,
    )


def test_replay_rebuilds_yesterday_today_and_fully_closed_lots():
    yesterday = date(2026, 8, 5)
    trades = [
        _trade("T1", trading_day=yesterday, offset="OPEN", price="100", volume=3),
        _trade(
            "T2",
            trading_day=DAY,
            offset="OPEN",
            price="105",
            volume=2,
            commission="1",
        ),
        _trade(
            "T3",
            trading_day=DAY,
            offset="CLOSE_YESTERDAY",
            price="110",
            volume=1,
            commission="2",
        ),
        _trade(
            "T4",
            trading_day=DAY,
            offset="CLOSE_TODAY",
            price="100",
            volume=2,
            commission="3",
        ),
    ]
    details = [
        _detail("D1", open_trade_id="T1", open_day=yesterday, price="100", volume=3),
        _detail("D2", open_trade_id="T2", open_day=DAY, price="105", volume=2),
    ]
    allocations = [
        _allocation(
            1,
            trade_id="T3",
            detail_id="D1",
            open_day=yesterday,
            open_price="100",
            close_price="110",
            volume=1,
            offset="CLOSE_YESTERDAY",
            commission="2",
        ),
        _allocation(
            2,
            trade_id="T4",
            detail_id="D2",
            open_day=DAY,
            open_price="105",
            close_price="100",
            volume=2,
            offset="CLOSE_TODAY",
            commission="3",
        ),
    ]
    prior = SimpleNamespace(settlement_price=Decimal("110"))

    result = SettlementReplayService().replay(
        trading_day=DAY,
        details=details,
        trades=trades,
        allocations=allocations,
        prior_position_settlements={"P1": prior},
        prior_expired_position_ids=set(),
        instruments={"RB2610": _instrument()},
        instruments_by_id={1: _instrument()},
        prices={("SHFE", "RB2610"): Decimal("120")},
        has_prior_batch=True,
    )

    position = result.positions[0]
    assert position.opening_yesterday_volume == 3
    assert position.today_open_volume == 2
    assert position.today_close_yesterday_volume == 1
    assert position.today_close_today_volume == 2
    assert position.ending_volume == 2
    assert position.holding_pnl == Decimal("20.000000")
    assert position.close_pnl == Decimal("-10.000000")
    assert position.commission == Decimal("6.000000")
    assert {item.position_detail_id: item.ending_volume for item in position.details} == {
        "D1": 2,
        "D2": 0,
    }


def test_non_expired_option_loss_is_recorded_as_economic_pnl():
    trade = _trade(
        "T1",
        trading_day=DAY,
        offset="OPEN",
        price="10",
        volume=1,
        instrument_type="FUTURES_OPTION",
    )
    detail = _detail(
        "D1", open_trade_id="T1", open_day=DAY, price="10", volume=1
    )
    detail.multiplier_snapshot = Decimal("10")
    instrument = _instrument(instrument_type="FUTURES_OPTION")

    result = SettlementReplayService().replay(
        trading_day=DAY,
        details=[detail],
        trades=[trade],
        allocations=[],
        prior_position_settlements={},
        prior_expired_position_ids=set(),
        instruments={"RB2610": instrument},
        instruments_by_id={1: instrument},
        prices={("SHFE", "RB2610"): Decimal("8")},
        has_prior_batch=False,
    )

    assert result.positions[0].option_economic_pnl == Decimal("-20.000000")
    assert result.positions[0].holding_pnl == Decimal("-20.000000")


def test_close_trade_without_allocation_is_rejected():
    with pytest.raises(DataAccessError) as error:
        SettlementReplayService().replay(
            trading_day=DAY,
            details=[],
            trades=[
                _trade(
                    "T1",
                    trading_day=DAY,
                    offset="CLOSE_YESTERDAY",
                    price="100",
                    volume=1,
                )
            ],
            allocations=[],
            prior_position_settlements={},
            prior_expired_position_ids=set(),
            instruments={},
            instruments_by_id={},
            prices={},
            has_prior_batch=False,
        )

    assert error.value.error_code == "REPLAY_CLOSE_ALLOCATION_INCONSISTENT"
