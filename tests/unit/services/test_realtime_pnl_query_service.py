from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from app.services.realtime_pnl_query_service import (
    RealtimePnlQueryService,
)


class FakeStore:
    def __init__(self, *, account=None, position=None):
        self.account = account or {}
        self.position = position or {}

    def get_account(self, _account_id):
        return self.account

    def get_position(self, _position_id):
        return self.position


def test_account_query_prefers_redis_realtime_snapshot():
    now = "2026-07-27T10:30:00+08:00"
    service = RealtimePnlQueryService(
        pnl_store=FakeStore(
            account={
                "account_id": "A001",
                "cumulative_unrealized_pnl": "1300.000000",
                "daily_position_pnl": "300.000000",
                "daily_close_pnl": "150.000000",
                "daily_commission": "6.000000",
                "daily_pnl": "444.000000",
                "equity": "101300.000000",
                "available_cash": "90000.000000",
                "risk_ratio": "0.10000000",
                "updated_at": now,
            }
        ),
    )

    result = service.get_account(SimpleNamespace(), "A001")

    assert result.unrealized_pnl == Decimal("1300.000000")
    assert result.daily_pnl == Decimal("444.000000")
    assert result.data_source == "REDIS_REALTIME"


def test_position_query_falls_back_to_postgres_snapshot():
    now = datetime(2026, 7, 27, tzinfo=timezone.utc)
    repository = SimpleNamespace(
        get_by_position_id=lambda _db, _position_id: SimpleNamespace(
            position_id="P001",
            account_id="A001",
            exchange_id="SHFE",
            symbol="RB2610",
            direction="LONG",
            unrealized_pnl=Decimal("1200"),
            daily_position_pnl=Decimal("200"),
            updated_at=now,
        )
    )
    service = RealtimePnlQueryService(
        pnl_store=FakeStore(),
        position_repository=repository,
    )

    result = service.get_position(SimpleNamespace(), "P001")

    assert result.mark_price is None
    assert result.unrealized_pnl == Decimal("1200")
    assert result.data_source == "POSTGRES_SNAPSHOT"
