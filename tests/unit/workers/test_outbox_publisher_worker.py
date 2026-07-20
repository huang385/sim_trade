from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.enums.order_enums import OutboxStatus
from app.models.outbox_event import OutboxEvent
from app.repositories.outbox_repository import OutboxRepository
from app.workers.outbox_publisher_worker import OutboxPublisherWorker


NOW = datetime(2026, 7, 20, tzinfo=timezone.utc)


class RecordingPublisher:
    def __init__(self, error=None):
        self.error = error
        self.event_ids = []

    def publish(self, event):
        self.event_ids.append(event.event_id)
        if self.error is not None:
            raise self.error
        return "1-0"


def make_session_factory():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    OutboxEvent.__table__.create(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def add_event(session_factory, *, event_id="EVT-1", max_retries=10):
    with session_factory() as db:
        OutboxRepository.create_event(
            db,
            event_id=event_id,
            aggregate_type="ORDER",
            aggregate_id="O-1",
            event_type="ORDER_ACCEPTED",
            payload={"event_id": event_id},
            created_at=NOW,
            max_retries=max_retries,
        )
        db.commit()


def get_event(session_factory, event_id="EVT-1"):
    with session_factory() as db:
        return db.scalar(
            select(OutboxEvent).where(OutboxEvent.event_id == event_id)
        )


def test_pending_event_is_published_and_marked_sent():
    session_factory = make_session_factory()
    add_event(session_factory)
    publisher = RecordingPublisher()
    worker = OutboxPublisherWorker(
        session_factory=session_factory,
        publisher=publisher,
    )

    result = worker.run_once()

    event = get_event(session_factory)
    assert result.claimed == 1
    assert result.sent == 1
    assert publisher.event_ids == ["EVT-1"]
    assert event.status == OutboxStatus.SENT.value
    assert event.sent_at is not None


def test_redis_failure_schedules_retry():
    session_factory = make_session_factory()
    add_event(session_factory)
    worker = OutboxPublisherWorker(
        session_factory=session_factory,
        publisher=RecordingPublisher(ConnectionError("redis down")),
    )

    result = worker.run_once()

    event = get_event(session_factory)
    assert result.retried == 1
    assert event.status == OutboxStatus.PENDING.value
    assert event.retry_count == 1
    assert event.next_retry_at is not None
    assert "redis down" in event.last_error


def test_sent_event_is_not_published_twice():
    session_factory = make_session_factory()
    add_event(session_factory)
    publisher = RecordingPublisher()
    worker = OutboxPublisherWorker(
        session_factory=session_factory,
        publisher=publisher,
    )

    first = worker.run_once()
    second = worker.run_once()

    assert first.sent == 1
    assert second.claimed == 0
    assert publisher.event_ids == ["EVT-1"]


def test_event_becomes_failed_at_retry_limit():
    session_factory = make_session_factory()
    add_event(session_factory, max_retries=1)
    worker = OutboxPublisherWorker(
        session_factory=session_factory,
        publisher=RecordingPublisher(ConnectionError("redis down")),
    )

    result = worker.run_once()

    event = get_event(session_factory)
    assert result.failed == 1
    assert event.status == OutboxStatus.FAILED.value
    assert event.retry_count == 1
    assert event.next_retry_at is None


def test_claimed_event_cannot_be_claimed_again_before_lease_expires():
    session_factory = make_session_factory()
    add_event(session_factory)
    repository = OutboxRepository()

    with session_factory() as first_db:
        first = repository.claim_pending_events(first_db, now=NOW)
        first_db.commit()
    with session_factory() as second_db:
        second = repository.claim_pending_events(second_db, now=NOW)

    assert [event.event_id for event in first] == ["EVT-1"]
    assert second == []
