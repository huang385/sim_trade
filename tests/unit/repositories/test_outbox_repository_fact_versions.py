from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.outbox_event import OutboxEvent
from app.repositories.outbox_repository import OutboxRepository


# SQLite的DateTime存储不保留时区，测试统一使用naive时间。
T1 = datetime(2026, 8, 21, 3, 0)
T2 = datetime(2026, 8, 21, 3, 5)
T3 = datetime(2026, 8, 21, 3, 10)


def _make_session():
    engine = create_engine("sqlite://")
    OutboxEvent.__table__.create(engine)
    return sessionmaker(bind=engine)()


def _add_event(db, oid, agg_type, agg_id, reason, created):
    db.add(
        OutboxEvent(
            id=oid,
            event_id=f"EVT-{oid}",
            aggregate_type=agg_type,
            aggregate_id=agg_id,
            event_type=(
                "ACCOUNT_FACT_UPDATED"
                if agg_type == "ACCOUNT"
                else "POSITION_UPDATED"
            ),
            payload={"fact_reason": reason} if reason else {},
            status="SENT",
            retry_count=0,
            max_retries=10,
            next_retry_at=None,
            last_error=None,
            created_at=created,
            sent_at=created,
            updated_at=created,
        )
    )


def _seed(db):
    # A001：id1(无reason) / id3(调整) / id5(无reason)
    _add_event(db, 1, "ACCOUNT", "A001", None, T1)
    # A002：id2(无reason) / id4(订单级调整)
    _add_event(db, 2, "ACCOUNT", "A002", None, T1)
    _add_event(db, 3, "ACCOUNT", "A001", "OPTION_MARGIN_ADJUSTMENT", T3)
    _add_event(db, 4, "ACCOUNT", "A002", "OPTION_ORDER_MARGIN_ADJUSTMENT", T3)
    _add_event(db, 5, "ACCOUNT", "A001", None, T2)
    # P001：id6(无reason) / id7(调整)
    _add_event(db, 6, "POSITION", "P001", None, T1)
    _add_event(db, 7, "POSITION", "P001", "OPTION_MARGIN_ADJUSTMENT", T3)
    db.commit()


def test_list_latest_fact_versions_excludes_requested_reasons():
    db = _make_session()
    _seed(db)

    versions = OutboxRepository.list_latest_fact_versions(
        db,
        account_ids=("A001", "A002"),
        position_ids=("P001",),
        exclude_fact_reasons=("OPTION_MARGIN_ADJUSTMENT",),
    )
    # A002 的 id4 是订单级调整，不在排除列表内，仍应计入。
    assert versions == {
        ("ACCOUNT", "A001"): "5",
        ("ACCOUNT", "A002"): "4",
        ("POSITION", "P001"): "6",
    }

    strict = OutboxRepository.list_latest_fact_versions(
        db,
        account_ids=("A001", "A002"),
        position_ids=("P001",),
    )
    assert strict == {
        ("ACCOUNT", "A001"): "5",
        ("ACCOUNT", "A002"): "4",
        ("POSITION", "P001"): "7",
    }


def test_list_latest_fact_created_times_follows_max_id_rows():
    db = _make_session()
    _seed(db)

    times = OutboxRepository.list_latest_fact_created_times(
        db,
        account_ids=("A001", "A002"),
        position_ids=("P001",),
        exclude_fact_reasons=(
            "OPTION_MARGIN_ADJUSTMENT",
            "OPTION_ORDER_MARGIN_ADJUSTMENT",
        ),
    )
    # 必须取最大id那一行的created_at，而不是max(created_at)：
    # A001 最大id=5(时间T2)，即使被排除的id3时间更晚(T3)。
    assert times == {
        ("ACCOUNT", "A001"): T2,
        ("ACCOUNT", "A002"): T1,
        ("POSITION", "P001"): T1,
    }


def test_latest_fact_queries_return_empty_without_ids():
    db = _make_session()
    _seed(db)

    assert OutboxRepository.list_latest_fact_versions(db) == {}
    assert OutboxRepository.list_latest_fact_created_times(db) == {}
