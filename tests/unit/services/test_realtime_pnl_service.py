from dataclasses import replace
from datetime import date
from decimal import Decimal
import json

from types import MappingProxyType

import pytest

from app.infrastructure.market_data.market_tick_store import MarketTickStore
from app.services.active_position_cache import (
    AccountPnlSnapshot,
    ActivePositionCycleSnapshot,
)
from app.services.pnl_calculator import (
    PnlDetailSnapshot,
    PositionPnlSnapshot,
)
from app.services.realtime_pnl_service import RealtimePnlService
from app.services.realtime_pnl_service import ContractPnlRequest


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

    def get_cycle_snapshot(self, **_kwargs):
        return ActivePositionCycleSnapshot(
            by_contract=MappingProxyType(
                {("SHFE", "RB2610"): (self.position,)}
            ),
            by_account=MappingProxyType({"A001": (self.position,)}),
            accounts=MappingProxyType({"A001": self.account}),
            cache_version="0",
            refresh_count=1,
        )


class FakeStore:
    def __init__(self):
        self.positions = {}
        self.accounts = {}
        self.calls = 0
        self.index_additions = []
        self.index_removals = []
        self.contract_ids = {}

    def get_position(self, position_id):
        return self.positions.get(position_id, {})

    def list_contract_position_ids(self, exchange_id, symbol):
        return set(
            self.contract_ids.get(
                (exchange_id, symbol),
                self.positions if len(self.positions) <= 1 else (),
            )
        )

    def list_contract_position_ids_many(self, contract_keys):
        return {
            key: self.list_contract_position_ids(*key)
            for key in contract_keys
        }

    def get_positions_many(self, position_ids):
        return {
            position_id: self.positions.get(position_id, {})
            for position_id in position_ids
        }

    def get_accounts_many(self, account_ids):
        return {
            account_id: self.accounts.get(account_id, {})
            for account_id in account_ids
        }

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

    def write_cycle_snapshots(
        self,
        *,
        positions,
        accounts,
        dirty_version,
        active_positions,
        closed_positions,
        expected_cache_version=None,
    ):
        self.index_additions.append(list(active_positions))
        self.index_removals.append(list(closed_positions))
        for _account_id, exchange_id, symbol, _order_book_id, position_id in active_positions:
            self.contract_ids.setdefault(
                (exchange_id, symbol),
                set(),
            ).add(position_id)
        for _account_id, exchange_id, symbol, _order_book_id, position_id in closed_positions:
            self.contract_ids.setdefault(
                (exchange_id, symbol),
                set(),
            ).discard(position_id)
        return self.write_snapshots(
            positions=positions,
            accounts=accounts,
            dirty_version=dirty_version,
        )


def make_fields(*, last_price="3520", ingest_type="LIVE_CALLBACK"):
    payload = {
        "source_event_id": "TICK-1",
        "source": "YMM_LIVE_DATA",
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


def test_old_trading_day_tick_cannot_overwrite_new_day_position():
    cache = FakeCache()
    next_day = date(2026, 7, 28)
    cache.position = replace(cache.position, trading_day=next_day)
    cache.account = replace(cache.account, trading_day=next_day)
    store = FakeStore()

    result = RealtimePnlService(
        active_position_cache=cache,
        pnl_store=store,
    ).process(stream_message_id="old-day-1", fields=make_fields())

    assert result.failed_contracts == frozenset({("SHFE", "RB2610")})
    assert "P001" not in store.positions


def test_futures_position_is_not_scheduled_for_option_margin_adjustment():
    cache = FakeCache()
    futures_position = replace(
        cache.position,
        persisted_used_margin=Decimal("10000"),
    )
    cycle = ActivePositionCycleSnapshot(
        by_contract=MappingProxyType(
            {("SHFE", "RB2610"): (futures_position,)}
        ),
        by_account=MappingProxyType({"A001": (futures_position,)}),
        accounts=MappingProxyType({"A001": cache.account}),
        cache_version="FUTURES-MARGIN",
        refresh_count=1,
    )

    result = RealtimePnlService(
        active_position_cache=cache,
        pnl_store=FakeStore(),
    ).process_batch(
        requests=[
            ContractPnlRequest(
                exchange_id="SHFE",
                symbol="RB2610",
                tick=RealtimePnlService.parse_tick(make_fields()),
            )
        ],
        cycle_snapshot=cycle,
        dirty_version="FUTURES-MARGIN-CYCLE",
    )

    assert result.margin_adjustment_positions == ()


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
    # 首次建立索引后，相同持仓的后续行情不再重复执行SADD。
    assert store.index_additions == [
        [("A001", "SHFE", "RB2610", "RB2610", "P001")],
        [],
    ]


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


def test_full_close_dirty_zeros_old_position_and_account():
    cache = FakeCache()
    cycle = cache.get_cycle_snapshot()
    cycle = ActivePositionCycleSnapshot(
        by_contract=MappingProxyType({}),
        by_account=MappingProxyType({"A001": ()}),
        accounts=cycle.accounts,
        cache_version="2",
        refresh_count=2,
    )
    store = FakeStore()
    store.positions["P001"] = {
        "position_id": "P001",
        "account_id": "A001",
        "exchange_id": "SHFE",
        "symbol": "RB2610",
        "direction": "LONG",
        "mark_price": "3520",
        "cumulative_unrealized_pnl": "2400.000000",
        "daily_position_pnl": "400.000000",
    }
    store.accounts["A001"] = {
        "cumulative_unrealized_pnl": "2400.000000",
        "daily_position_pnl": "400.000000",
    }
    service = RealtimePnlService(
        active_position_cache=cache,
        pnl_store=store,
    )

    result = service.process_batch(
        requests=[
            ContractPnlRequest(
                exchange_id="SHFE",
                symbol="RB2610",
                tick=None,
                dirty_version="2",
                dirty_account_ids=frozenset({"A001"}),
            )
        ],
        cycle_snapshot=cycle,
        dirty_version="cycle-2",
    )

    assert result.successful_contracts == {("SHFE", "RB2610")}
    assert store.positions["P001"][
        "cumulative_unrealized_pnl"
    ] == "0.000000"
    assert store.accounts["A001"][
        "cumulative_unrealized_pnl"
    ] == "0.000000"
    assert store.index_removals[-1] == [
        ("A001", "SHFE", "RB2610", "RB2610", "P001")
    ]


def test_partial_close_dirty_recalculates_same_price_with_less_volume():
    cache = FakeCache()
    reduced = PositionPnlSnapshot(
        position_id="P001",
        account_id="A001",
        order_book_id="RB2610",
        exchange_id="SHFE",
        symbol="RB2610",
        direction="LONG",
        contract_multiplier=Decimal("10"),
        persisted_unrealized_pnl=Decimal("2400"),
        persisted_daily_position_pnl=Decimal("400"),
        details=(
            PnlDetailSnapshot(
                "PD001",
                Decimal("3400"),
                Decimal("3500"),
                1,
            ),
        ),
    )
    cycle = ActivePositionCycleSnapshot(
        by_contract=MappingProxyType(
            {("SHFE", "RB2610"): (reduced,)}
        ),
        by_account=MappingProxyType({"A001": (reduced,)}),
        accounts=MappingProxyType({"A001": cache.account}),
        cache_version="3",
        refresh_count=3,
    )
    store = FakeStore()
    store.positions["P001"] = {
        "position_id": "P001",
        "account_id": "A001",
        "exchange_id": "SHFE",
        "symbol": "RB2610",
        "direction": "LONG",
        "mark_price": "3520",
        "cumulative_unrealized_pnl": "2400.000000",
        "daily_position_pnl": "400.000000",
    }
    store.accounts["A001"] = {
        "cumulative_unrealized_pnl": "2400.000000",
        "daily_position_pnl": "400.000000",
    }
    tick = RealtimePnlService.parse_tick(make_fields())

    RealtimePnlService(
        active_position_cache=cache,
        pnl_store=store,
    ).process_batch(
        requests=[
            ContractPnlRequest(
                exchange_id="SHFE",
                symbol="RB2610",
                tick=tick,
                dirty_version="3",
                dirty_account_ids=frozenset({"A001"}),
            )
        ],
        cycle_snapshot=cycle,
        dirty_version="cycle-3",
    )

    assert store.positions["P001"][
        "cumulative_unrealized_pnl"
    ] == "1200.000000"
    assert store.accounts["A001"][
        "cumulative_unrealized_pnl"
    ] == "1200.000000"


def test_two_contract_deltas_update_same_account_only_once():
    cache = FakeCache()

    def position(position_id, symbol, open_price):
        return PositionPnlSnapshot(
            position_id=position_id,
            account_id="A001",
            order_book_id=symbol,
            exchange_id="DCE",
            symbol=symbol,
            direction="LONG",
            contract_multiplier=Decimal("1"),
            persisted_unrealized_pnl=Decimal("0"),
            persisted_daily_position_pnl=Decimal("0"),
            details=(
                PnlDetailSnapshot(
                    f"D-{position_id}",
                    Decimal(open_price),
                    Decimal(open_price),
                    1,
                ),
            ),
        )

    first = position("P1", "JD2609", "100")
    second = position("P2", "JM2609", "200")
    cycle = ActivePositionCycleSnapshot(
        by_contract=MappingProxyType(
            {
                ("DCE", "JD2609"): (first,),
                ("DCE", "JM2609"): (second,),
            }
        ),
        by_account=MappingProxyType({"A001": (first, second)}),
        accounts=MappingProxyType({"A001": cache.account}),
        cache_version="4",
        refresh_count=4,
    )
    store = FakeStore()
    store.positions = {
        "P1": {
            "account_id": "A001",
            "cumulative_unrealized_pnl": "5",
            "daily_position_pnl": "5",
        },
        "P2": {
            "account_id": "A001",
            "cumulative_unrealized_pnl": "10",
            "daily_position_pnl": "10",
        },
    }
    store.accounts["A001"] = {
        "cumulative_unrealized_pnl": "15",
        "daily_position_pnl": "15",
    }
    store.contract_ids = {
        ("DCE", "JD2609"): {"P1"},
        ("DCE", "JM2609"): {"P2"},
    }

    def tick(symbol, price, sequence):
        fields = make_fields(last_price=price)
        payload = json.loads(fields["payload"])
        payload.update(
            {
                "order_book_id": symbol,
                "exchange_id": "DCE",
                "symbol": symbol,
                "source_event_id": f"T-{sequence}",
                "sequence_id": sequence,
            }
        )
        return RealtimePnlService.parse_tick(
            {
                "event_type": "MARKET_TICK",
                "payload": json.dumps(payload),
            }
        )

    result = RealtimePnlService(
        active_position_cache=cache,
        pnl_store=store,
    ).process_batch(
        requests=[
            ContractPnlRequest(
                exchange_id="DCE",
                symbol="JD2609",
                tick=tick("JD2609", "110", 1),
            ),
            ContractPnlRequest(
                exchange_id="DCE",
                symbol="JM2609",
                tick=tick("JM2609", "220", 2),
            ),
        ],
        cycle_snapshot=cycle,
        dirty_version="cycle-4",
    )

    assert result.accounts_updated == 1
    assert store.calls == 1
    assert store.accounts["A001"][
        "cumulative_unrealized_pnl"
    ] == "30.000000"


def test_account_fact_dirty_reuses_existing_pnl_without_contract_rebuild():
    cache = FakeCache()
    refreshed_account = AccountPnlSnapshot(
        account_id="A001",
        cash_balance=Decimal("100000"),
        used_margin=Decimal("10000"),
        frozen_margin=Decimal("5000"),
        frozen_cash=Decimal("0"),
        frozen_commission=Decimal("10"),
        unrealized_pnl=Decimal("0"),
        daily_position_pnl=Decimal("0"),
        daily_close_pnl=Decimal("150"),
        daily_commission=Decimal("6"),
    )
    cycle = ActivePositionCycleSnapshot(
        by_contract=MappingProxyType(
            {("SHFE", "RB2610"): (cache.position,)}
        ),
        by_account=MappingProxyType({"A001": (cache.position,)}),
        accounts=MappingProxyType({"A001": refreshed_account}),
        cache_version="4",
        refresh_count=2,
    )
    store = FakeStore()
    store.accounts["A001"] = {
        "cumulative_unrealized_pnl": "2400.000000",
        "daily_position_pnl": "400.000000",
    }

    result = RealtimePnlService(
        active_position_cache=cache,
        pnl_store=store,
    ).process_batch(
        requests=[],
        cycle_snapshot=cycle,
        dirty_version="cycle-account",
        account_fact_versions={"A001": "7"},
    )

    assert result.successful_account_facts == {"A001"}
    assert result.positions_calculated == 0
    assert store.positions == {}
    assert store.accounts["A001"][
        "cumulative_unrealized_pnl"
    ] == "2400.000000"
    assert store.accounts["A001"]["available_cash"] == "87390.000000"


@pytest.mark.parametrize(
    (
        "booked_margin",
        "instrument_type",
        "margin_algorithm",
        "expected_required_margin",
        "expected_risk_available",
    ),
    [
        (
            "1000",
            "FUTURES_OPTION",
            "COMMODITY_FUTURES_OPTION",
            "1250.000000",
            "97550.000000",
        ),
        (
            "1500",
            "FUTURES_OPTION",
            "COMMODITY_FUTURES_OPTION",
            "1250.000000",
            "97300.000000",
        ),
        (
            "1000",
            "INDEX_OPTION",
            "CFFEX_INDEX_OPTION",
            "1205.000000",
            "97595.000000",
        ),
        (
            "1500",
            "INDEX_OPTION",
            "CFFEX_INDEX_OPTION",
            "1205.000000",
            "97300.000000",
        ),
    ],
)
def test_option_long_short_market_value_and_margin_are_exact(
    booked_margin,
    instrument_type,
    margin_algorithm,
    expected_required_margin,
    expected_risk_available,
):
    """同账户期权多空持仓只写一次快照，并精确核对权益与风险保证金。"""

    def option_position(position_id, direction, volume, open_price):
        return PositionPnlSnapshot(
            position_id=position_id,
            account_id="A001",
            order_book_id="AGOPT",
            exchange_id="SHFE",
            symbol="AGOPT",
            direction=direction,
            contract_multiplier=Decimal("10"),
            persisted_unrealized_pnl=Decimal("0"),
            persisted_daily_position_pnl=Decimal("0"),
            details=(
                PnlDetailSnapshot(
                    f"D-{position_id}",
                    Decimal(open_price),
                    Decimal(open_price),
                    volume,
                ),
            ),
            instrument_type=instrument_type,
            total_volume=volume,
            persisted_realtime_required_margin=(
                Decimal("1000") if direction == "SHORT" else Decimal("0")
            ),
            persisted_used_margin=(
                Decimal(booked_margin)
                if direction == "SHORT"
                else Decimal("0")
            ),
            option_type="CALL",
            strike_price=Decimal("110"),
            underlying_exchange_id="SHFE",
            underlying_symbol="AG",
            margin_rule_snapshot=tuple(
                {
                    "rule_id": "1",
                    "rule_version": "V1",
                    "margin_algorithm": margin_algorithm,
                    "margin_adjustment_rate": "0.1",
                    "minimum_guarantee_rate": "0.05",
                    "out_of_money_deduction_rate": "1",
                    "minimum_underlying_margin_ratio": "0.5",
                    "extra_margin_rate": "0",
                    "underlying_margin_rate": "0.1",
                    "underlying_multiplier": "10",
                }.items()
            ),
        )

    long_position = option_position("PL", "LONG", 2, "100")
    short_position = option_position("PS", "SHORT", 1, "110")
    account = AccountPnlSnapshot(
        account_id="A001",
        cash_balance=Decimal("100000"),
        used_margin=Decimal(booked_margin),
        frozen_margin=Decimal("0"),
        frozen_cash=Decimal("0"),
        frozen_commission=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        daily_position_pnl=Decimal("0"),
        daily_close_pnl=Decimal("0"),
        daily_commission=Decimal("0"),
        option_used_margin=Decimal(booked_margin),
        option_realtime_required_margin=Decimal(booked_margin),
        risk_state="MARGIN_DEFICIT",
    )
    cycle = ActivePositionCycleSnapshot(
        by_contract=MappingProxyType(
            {("SHFE", "AGOPT"): (long_position, short_position)}
        ),
        by_account=MappingProxyType(
            {"A001": (long_position, short_position)}
        ),
        accounts=MappingProxyType({"A001": account}),
        cache_version="OPTION-1",
        refresh_count=1,
    )

    option_payload = json.loads(make_fields(last_price="120")["payload"])
    option_payload.update(
        {
            "source_event_id": "OPT-1",
            "order_book_id": "AGOPT",
            "exchange_id": "SHFE",
            "symbol": "AGOPT",
        }
    )
    option_tick = RealtimePnlService.parse_tick(
        {
            "event_type": "MARKET_TICK",
            "payload": json.dumps(option_payload),
        }
    )
    underlying_payload = dict(option_payload)
    underlying_payload.update(
        {
            "source_event_id": "UNDERLYING-1",
            "order_book_id": "AG",
            "symbol": "AG",
            "last_price": "100",
        }
    )
    underlying_tick = RealtimePnlService.parse_tick(
        {
            "event_type": "MARKET_TICK",
            "payload": json.dumps(underlying_payload),
        }
    )

    class LatestMarket:
        def get_latest_many(self, _keys):
            return {
                ("SHFE", "AG"): MarketTickStore.tick_to_mapping(
                    underlying_tick
                )
            }

    store = FakeStore()
    result = RealtimePnlService(
        active_position_cache=FakeCache(),
        pnl_store=store,
        market_tick_store=LatestMarket(),
    ).process_batch(
        requests=[
            ContractPnlRequest(
                exchange_id="SHFE",
                symbol="AGOPT",
                tick=option_tick,
            )
        ],
        cycle_snapshot=cycle,
        dirty_version="OPTION-CYCLE",
    )

    # 多头2手市值2400，空头1手市值1200；账户权益净增加1200。
    assert store.accounts["A001"]["long_option_market_value"] == "2400.000000"
    assert (
        store.accounts["A001"]["short_option_market_value"]
        == "1200.000000"
    )
    assert store.accounts["A001"]["equity"] == "101200.000000"
    # 空头每手：权利金1200 + 最低风险50 = 1250。
    assert (
        store.accounts["A001"]["option_realtime_required_margin"]
        == expected_required_margin
    )
    assert (
        store.accounts["A001"]["risk_available_cash"]
        == expected_risk_available
    )
    assert store.accounts["A001"]["daily_position_pnl"] == "300.000000"
    assert store.accounts["A001"]["risk_state"] == "MARGIN_DEFICIT"
    assert result.accounts_updated == 1
    assert result.margin_adjustment_positions == (
        ("A001", "PS", ("SHFE", "AGOPT")),
    )
