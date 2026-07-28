from datetime import datetime
from decimal import Decimal
import json

from app.services.active_position_cache import AccountPnlSnapshot
from app.services.pnl_calculator import (
    PnlDetailSnapshot,
    PositionPnlSnapshot,
)
from app.services.realtime_pnl_service import RealtimePnlService


class FakeCache:
    def __init__(self):
        self.position = PositionPnlSnapshot(
            position_id="P001",
            account_id="A001",
            order_book_id="RB2610",
            exchange_id="SHFE",
            symbol="RB2610",
            direction="LONG",
            contract_multiplier=Decimal("10"),
            persisted_unrealized_pnl=Decimal("0"),
            persisted_daily_position_pnl=Decimal("0"),
            details=(
                PnlDetailSnapshot(
                    "PD001",
                    Decimal("3400"),
                    Decimal("3500"),
                    2,
                ),
            ),
        )
        self.account = AccountPnlSnapshot(
            account_id="A001",
            cash_balance=Decimal("100000"),
            used_margin=Decimal("10000"),
            frozen_margin=Decimal("0"),
            frozen_cash=Decimal("0"),
            frozen_commission=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            daily_position_pnl=Decimal("0"),
            daily_close_pnl=Decimal("150"),
            daily_commission=Decimal("6"),
        )

    def get_by_contract(self, exchange_id, symbol):
        if (exchange_id, symbol) == ("SHFE", "RB2610"):
            return (self.position,)
        return ()

    def get_by_account(self, account_id):
        return (self.position,) if account_id == "A001" else ()

    def get_account(self, account_id):
        return self.account if account_id == "A001" else None


class FakeStore:
    def __init__(self):
        self.positions = {}
        self.accounts = {}
        self.calls = 0

    def get_position(self, position_id):
        return self.positions.get(position_id, {})

    def write_snapshots(
        self,
        *,
        positions,
        accounts,
        dirty_version,
    ):
        self.calls += 1
        self.positions.update(
            {
                item.position_id: {
                    key: str(value)
                    for key, value in item.model_dump().items()
                }
                for item in positions
            }
        )
        self.accounts.update(
            {
                item.account_id: {
                    key: str(value)
                    for key, value in item.model_dump().items()
                }
                for item in accounts
            }
        )
        return len(positions), len(accounts)


def make_fields(*, last_price="3520", ingest_type="LIVE_CALLBACK"):
    payload = {
        "source_event_id": "TICK-1",
        "source": "YML_FEEDHUB",
        "ingest_type": ingest_type,
        "order_book_id": "RB2610",
        "exchange_id": "SHFE",
        "symbol": "RB2610",
        "trading_day": "2026-07-27",
        "event_time": "2026-07-27T10:30:00+08:00",
        "sequence_id": 1,
        "last_price": last_price,
        "cumulative_volume": 1,
        "bid_volume_1": 1,
        "ask_volume_1": 1,
    }
    return {
        "event_type": "MARKET_TICK",
        "payload": json.dumps(payload),
    }


def test_tick_writes_absolute_position_and_account_snapshots():
    store = FakeStore()
    service = RealtimePnlService(
        active_position_cache=FakeCache(),
        pnl_store=store,
    )

    result = service.process(
        stream_message_id="1-0",
        fields=make_fields(),
    )

    assert result.action == "CALCULATED"
    assert store.positions["P001"][
        "cumulative_unrealized_pnl"
    ] == "2400.000000"
    assert store.positions["P001"][
        "daily_position_pnl"
    ] == "400.000000"
    assert store.accounts["A001"]["daily_pnl"] == "544.000000"


def test_duplicate_tick_overwrites_same_absolute_values():
    store = FakeStore()
    service = RealtimePnlService(
        active_position_cache=FakeCache(),
        pnl_store=store,
    )

    service.process(stream_message_id="1-0", fields=make_fields())
    first = dict(store.positions["P001"])
    service.process(stream_message_id="1-0", fields=make_fields())

    assert store.calls == 2
    # 快照更新时间允许刷新，但全部资金字段必须仍是同一组绝对值。
    current = dict(store.positions["P001"])
    first.pop("updated_at")
    current.pop("updated_at")
    assert current == first


def test_rest_or_empty_price_is_skipped_without_redis_write():
    store = FakeStore()
    service = RealtimePnlService(
        active_position_cache=FakeCache(),
        pnl_store=store,
    )

    rest = service.process(
        stream_message_id="1-0",
        fields=make_fields(ingest_type="REST_SNAPSHOT"),
    )
    empty = service.process(
        stream_message_id="2-0",
        fields=make_fields(last_price=None),
    )

    assert rest.action == "SKIPPED"
    assert empty.action == "SKIPPED"
    assert store.calls == 0


def test_one_thousand_ticks_only_write_realtime_store_not_database():
    """
    RealtimePnlService没有Session依赖；连续1000条Tick只覆盖Redis绝对快照，
    PostgreSQL写入统一留给定时持久化Worker。
    """

    store = FakeStore()
    service = RealtimePnlService(
        active_position_cache=FakeCache(),
        pnl_store=store,
    )

    for index in range(1000):
        service.process(
            stream_message_id=f"{index}-0",
            fields=make_fields(last_price=str(3500 + index)),
        )

    assert store.calls == 1000
    assert store.positions["P001"][
        "cumulative_unrealized_pnl"
    ] == "21980.000000"
