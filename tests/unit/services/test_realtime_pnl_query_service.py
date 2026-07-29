from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

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

    def get_account_with_positions(
        self,
        *,
        account_id,
        position_ids,
    ):
        return self.account, {
            position_id: self.position
            for position_id in position_ids
            if self.position
        }


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


def _account(now):
    return SimpleNamespace(
        id=1,
        account_id="A001",
        user_id="U001",
        account_name="批量快照测试",
        account_type="FUTURES",
        initial_cash=Decimal("100000"),
        cash_balance=Decimal("100000"),
        available_cash=Decimal("90000"),
        frozen_cash=Decimal("0"),
        equity=Decimal("101000"),
        used_margin=Decimal("10000"),
        frozen_margin=Decimal("0"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("1000"),
        daily_position_pnl=Decimal("1000"),
        daily_close_pnl=Decimal("0"),
        daily_commission=Decimal("10"),
        daily_pnl=Decimal("990"),
        used_commission=Decimal("10"),
        frozen_commission=Decimal("0"),
        risk_ratio=Decimal("0.1"),
        status="ACTIVE",
        trading_day=None,
        created_at=now,
        updated_at=now,
    )


def _position(now, position_id):
    return SimpleNamespace(
        position_id=position_id,
        account_id="A001",
        order_book_id="JD2609",
        exchange_id="DCE",
        symbol="JD2609",
        direction="LONG",
        total_volume=1,
        today_volume=1,
        yesterday_volume=0,
        frozen_volume=0,
        available_volume=1,
        average_open_price=Decimal("3000"),
        position_cost=Decimal("30000"),
        used_margin=Decimal("3000"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("100"),
        daily_position_pnl=Decimal("100"),
        daily_close_pnl=Decimal("0"),
        trading_day=now.date(),
        created_at=now,
        updated_at=now,
    )


def test_account_trading_snapshot_batches_postgres_and_redis_reads():
    now = datetime(2026, 7, 29, tzinfo=timezone.utc)
    account_repository = Mock()
    account_repository.get_by_account_id.return_value = _account(now)
    position_repository = Mock()
    position_repository.list_by_account.return_value = [
        _position(now, "P001"),
        _position(now, "P002"),
    ]
    pnl_store = Mock()
    pnl_store.get_account_with_positions.return_value = (
        {
            "account_id": "A001",
            "cumulative_unrealized_pnl": "1200",
            "daily_position_pnl": "1200",
            "daily_close_pnl": "0",
            "daily_commission": "10",
            "daily_pnl": "1190",
            "equity": "101200",
            "available_cash": "90200",
            "risk_ratio": "0.0988",
            "updated_at": now.isoformat(),
        },
        {},
    )
    service = RealtimePnlQueryService(
        pnl_store=pnl_store,
        account_repository=account_repository,
        position_repository=position_repository,
    )

    result = service.get_account_trading_snapshot(
        SimpleNamespace(),
        "A001",
    )

    assert result.pnl.data_source == "REDIS_REALTIME"
    assert [item.position.position_id for item in result.positions] == [
        "P001",
        "P002",
    ]
    # Redis持仓快照缺失时直接复用同批数据库持仓，不再按position_id查询。
    assert all(
        item.pnl.data_source == "POSTGRES_SNAPSHOT"
        for item in result.positions
    )
    account_repository.get_by_account_id.assert_called_once()
    position_repository.list_by_account.assert_called_once()
    position_repository.get_by_position_id.assert_not_called()
    pnl_store.get_account_with_positions.assert_called_once_with(
        account_id="A001",
        position_ids=["P001", "P002"],
    )


def test_account_queries_reuse_preloaded_authorized_account():
    """授权层已加载账户时，实时盈亏服务不得再次查询同一账户。"""

    now = datetime(2026, 7, 29, tzinfo=timezone.utc)
    account = _account(now)
    account_repository = Mock()
    position_repository = Mock()
    position_repository.list_by_account.return_value = []
    service = RealtimePnlQueryService(
        pnl_store=FakeStore(),
        account_repository=account_repository,
        position_repository=position_repository,
    )

    pnl = service.get_account(
        SimpleNamespace(),
        "A001",
        account=account,
    )
    snapshot = service.get_account_trading_snapshot(
        SimpleNamespace(),
        "A001",
        account=account,
    )

    assert pnl.account_id == "A001"
    assert snapshot.account.account_id == "A001"
    account_repository.get_by_account_id.assert_not_called()
