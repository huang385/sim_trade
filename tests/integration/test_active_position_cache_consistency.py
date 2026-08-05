from decimal import Decimal
from threading import Event, Thread
from uuid import uuid4

from sqlalchemy import delete, select, text

from app.common.time_utils import utc_now
from app.core.database import SessionLocal
from app.models.account import Account
from app.models.outbox_event import OutboxEvent
from app.repositories.account_repository import AccountRepository
from app.repositories.outbox_repository import OutboxRepository
from app.repositories.position_repository import PositionRepository
from app.services.active_position_cache import ActivePositionCache
from app.services.realtime_fact_event_service import RealtimeFactEventService


class BlockingPositionRepository(PositionRepository):
    """在完整持仓查询后暂停，稳定复现并发提交窗口。"""

    def __init__(self, rows_read: Event, resume: Event):
        self.rows_read = rows_read
        self.resume = resume
        self.block = True
        self.transaction_settings = None

    def list_active_calculation_rows(self, db):
        rows = super().list_active_calculation_rows(db)
        self.transaction_settings = (
            db.scalar(text("SHOW transaction_isolation")),
            db.scalar(text("SHOW transaction_read_only")),
        )
        if self.block:
            self.rows_read.set()
            assert self.resume.wait(10), "等待并发提交超时"
        return rows


class BlockingAccountRepository(AccountRepository):
    """在增量账户查询后暂停，确保版本查询发生在并发提交之后。"""

    def __init__(self, rows_read: Event, resume: Event):
        self.rows_read = rows_read
        self.resume = resume
        self.block = False

    def list_by_account_ids(self, db, account_ids):
        accounts = super().list_by_account_ids(db, account_ids)
        if self.block:
            self.rows_read.set()
            assert self.resume.wait(10), "等待并发提交超时"
        return accounts


def _create_account_fact(account_id: str, event_id: str) -> str:
    with SessionLocal() as db:
        event = OutboxRepository.create_event(
            db,
            event_id=event_id,
            aggregate_type="ACCOUNT",
            aggregate_id=account_id,
            event_type="ACCOUNT_FACT_UPDATED",
            payload={
                "event_id": event_id,
                "event_type": "ACCOUNT_FACT_UPDATED",
                "account_id": account_id,
            },
            created_at=utc_now(),
        )
        db.flush()
        version = str(event.id)
        db.commit()
        return version


def _update_account_and_create_fact(
    account_id: str,
    event_id: str,
    frozen_margin: Decimal,
) -> str:
    with SessionLocal() as db:
        account = db.scalar(
            select(Account)
            .where(Account.account_id == account_id)
            .with_for_update()
        )
        account.frozen_margin = frozen_margin
        account.updated_at = utc_now()
        service = RealtimeFactEventService(
            repository=OutboxRepository(),
            event_id_factory=lambda: event_id,
        )
        service.create_account_updated(
            db,
            account=account,
            occurred_at=account.updated_at,
        )
        db.flush()
        event = db.scalar(
            select(OutboxEvent).where(OutboxEvent.event_id == event_id)
        )
        version = str(event.id)
        db.commit()
        return version


def test_full_reload_never_combines_old_rows_with_new_outbox_version(
    integration_context,
):
    """真实PostgreSQL验证完整加载只会得到旧+旧或新+新。"""

    suffix = uuid4().hex
    old_event_id = f"CACHE-OLD-{suffix}"
    new_event_id = f"CACHE-NEW-{suffix}"
    old_version = _create_account_fact(
        integration_context.account_id,
        old_event_id,
    )
    rows_read = Event()
    resume = Event()
    positions = BlockingPositionRepository(rows_read, resume)
    cache = ActivePositionCache(
        session_factory=SessionLocal,
        position_repository=positions,
        account_repository=AccountRepository(),
        outbox_repository=OutboxRepository(),
        refresh_ms=60_000,
    )
    result = {}

    def load_cache():
        try:
            result["snapshot"] = cache.get_cycle_snapshot(
                extra_account_ids={integration_context.account_id}
            )
        except Exception as exc:  # pragma: no cover - 仅用于跨线程回传
            result["error"] = exc

    thread = Thread(target=load_cache, daemon=True)
    thread.start()
    assert rows_read.wait(10), "活动持仓查询未进入并发窗口"
    new_version = _update_account_and_create_fact(
        integration_context.account_id,
        new_event_id,
        Decimal("321.000000"),
    )
    resume.set()
    thread.join(10)
    assert not thread.is_alive(), "一致性读取事务未结束"
    assert "error" not in result

    old_snapshot = result["snapshot"].get_account(
        integration_context.account_id
    )
    assert old_snapshot.frozen_margin == Decimal("0")
    assert old_snapshot.source_fact_version == old_version
    assert positions.transaction_settings == ("repeatable read", "on")

    fresh = ActivePositionCache(
        session_factory=SessionLocal,
        refresh_ms=60_000,
    ).get_cycle_snapshot(
        extra_account_ids={integration_context.account_id}
    )
    assert fresh.get_account(
        integration_context.account_id
    ).frozen_margin == Decimal("321.000000")
    assert fresh.get_account(
        integration_context.account_id
    ).source_fact_version == new_version

    with SessionLocal() as db:
        db.execute(
            delete(OutboxEvent).where(
                OutboxEvent.event_id.in_((old_event_id, new_event_id))
            )
        )
        account = db.scalar(
            select(Account).where(
                Account.account_id == integration_context.account_id
            )
        )
        account.frozen_margin = Decimal("0")
        db.commit()


def test_incremental_refresh_uses_same_mvcc_snapshot_for_account_and_version(
    integration_context,
):
    """真实PostgreSQL验证增量账户事实和Outbox版本也保持一致。"""

    suffix = uuid4().hex
    old_event_id = f"CACHE-I-OLD-{suffix}"
    new_event_id = f"CACHE-I-NEW-{suffix}"
    old_version = _create_account_fact(
        integration_context.account_id,
        old_event_id,
    )
    rows_read = Event()
    resume = Event()
    accounts = BlockingAccountRepository(rows_read, resume)
    cache = ActivePositionCache(
        session_factory=SessionLocal,
        account_repository=accounts,
        refresh_ms=60_000,
    )
    cache.get_cycle_snapshot(
        extra_account_ids={integration_context.account_id}
    )
    accounts.block = True
    result = {}

    def refresh_cache():
        try:
            result["snapshot"] = cache.get_cycle_snapshot(
                extra_account_ids={integration_context.account_id},
                refresh_account_versions={
                    integration_context.account_id: "DIRTY-2"
                },
            )
        except Exception as exc:  # pragma: no cover - 仅用于跨线程回传
            result["error"] = exc

    thread = Thread(target=refresh_cache, daemon=True)
    thread.start()
    assert rows_read.wait(10), "账户查询未进入并发窗口"
    new_version = _update_account_and_create_fact(
        integration_context.account_id,
        new_event_id,
        Decimal("654.000000"),
    )
    resume.set()
    thread.join(10)
    assert not thread.is_alive(), "增量一致性读取事务未结束"
    assert "error" not in result

    snapshot = result["snapshot"].get_account(
        integration_context.account_id
    )
    assert snapshot.frozen_margin == Decimal("0")
    assert snapshot.source_fact_version == old_version

    fresh = ActivePositionCache(
        session_factory=SessionLocal,
        refresh_ms=60_000,
    ).get_cycle_snapshot(extra_account_ids={integration_context.account_id})
    assert fresh.get_account(
        integration_context.account_id
    ).frozen_margin == Decimal("654.000000")
    assert fresh.get_account(
        integration_context.account_id
    ).source_fact_version == new_version

    with SessionLocal() as db:
        db.execute(
            delete(OutboxEvent).where(
                OutboxEvent.event_id.in_((old_event_id, new_event_id))
            )
        )
        account = db.scalar(
            select(Account).where(
                Account.account_id == integration_context.account_id
            )
        )
        account.frozen_margin = Decimal("0")
        db.commit()
