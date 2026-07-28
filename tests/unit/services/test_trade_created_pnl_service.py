from datetime import datetime, timezone
from decimal import Decimal
import json
from types import SimpleNamespace

from app.services.trade_created_pnl_service import (
    TradeCreatedPnlService,
)


class FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakeCache:
    def __init__(self):
        self.invalidated = False

    def invalidate(self, **_kwargs):
        self.invalidated = True

    def get_by_account(self, _account_id):
        return ()


class FakeStore:
    def __init__(self):
        self.positions = []
        self.accounts = []
        self.removed = []
        self.version = 0

    def bump_position_cache_version(self):
        self.version += 1
        return str(self.version)

    def list_contract_position_ids(self, _exchange_id, _symbol):
        return {"P001"}

    def write_snapshots(
        self,
        *,
        positions,
        accounts,
        dirty_version,
    ):
        self.positions.extend(positions)
        self.accounts.extend(accounts)
        return len(positions), len(accounts)

    def remove_contract_position(self, **kwargs):
        self.removed.append(kwargs)


def test_full_close_trade_immediately_zeroes_position_and_account_snapshot():
    now = datetime(2026, 7, 27, 10, tzinfo=timezone.utc)
    account = SimpleNamespace(
        account_id="A001",
        cash_balance=Decimal("100000"),
        used_margin=Decimal("0"),
        frozen_margin=Decimal("0"),
        frozen_cash=Decimal("0"),
        frozen_commission=Decimal("0"),
        daily_close_pnl=Decimal("150"),
        daily_commission=Decimal("6"),
    )
    closed_position = SimpleNamespace(
        position_id="P001",
        account_id="A001",
        exchange_id="SHFE",
        symbol="RB2610",
        direction="LONG",
        total_volume=0,
    )
    store = FakeStore()
    cache = FakeCache()
    service = TradeCreatedPnlService(
        session_factory=FakeSession,
        cache=cache,
        pnl_store=store,
        market_tick_store=SimpleNamespace(
            get_latest=lambda *_args: {}
        ),
        realtime_service=SimpleNamespace(),
        account_repository=SimpleNamespace(
            get_by_account_id=lambda *_args: account
        ),
        position_repository=SimpleNamespace(
            list_by_account_contract=lambda *_args, **_kwargs: [
                closed_position
            ]
        ),
    )
    fields = {
        "event_type": "TRADE_CREATED",
        "payload": json.dumps(
            {
                "event_id": "E001",
                "account_id": "A001",
                "exchange_id": "SHFE",
                "symbol": "RB2610",
                "trade_price": "3520",
                "trade_time": now.isoformat(),
            }
        ),
    }

    result = service.process(
        stream_message_id="1-0",
        fields=fields,
    )

    assert result.action == "REFRESHED"
    assert cache.invalidated is True
    assert store.version == 1
    assert store.positions[0].cumulative_unrealized_pnl == Decimal(
        "0.000000"
    )
    assert store.positions[0].daily_position_pnl == Decimal("0.000000")
    assert store.accounts[0].cumulative_unrealized_pnl == Decimal(
        "0.000000"
    )
    assert store.accounts[0].daily_pnl == Decimal("144.000000")
    assert store.removed[0]["position_id"] == "P001"
