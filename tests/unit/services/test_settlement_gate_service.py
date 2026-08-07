from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.common.exceptions import BusinessRuleError
from app.models.daily_settlement import DailySettlementBatch
from app.services.settlement_gate_service import SettlementGateService


def _batch_table_engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    DailySettlementBatch.__table__.create(engine)
    return engine


def _batch(*, status: str) -> DailySettlementBatch:
    now = datetime(2026, 8, 6, tzinfo=timezone.utc)
    return DailySettlementBatch(
        batch_id=f"B-{status}",
        trading_day=date(2026, 8, 6),
        status=status,
        current_stage="PREFLIGHT",
        started_at=now,
        cache_status="PENDING",
        created_at=now,
        updated_at=now,
    )


def test_running_batch_closes_database_trading_gate():
    engine = _batch_table_engine()
    with Session(engine) as db:
        db.add(_batch(status="RUNNING"))
        db.commit()

        with pytest.raises(BusinessRuleError) as raised:
            SettlementGateService().ensure_trading_open(
                db,
                trading_day=date(2026, 8, 7),
            )

    assert raised.value.error_code == "DAILY_SETTLEMENT_TRADING_CLOSED"
    engine.dispose()


def test_completed_batch_blocks_old_day_but_opens_next_day():
    engine = _batch_table_engine()
    with Session(engine) as db:
        db.add(_batch(status="COMPLETED"))
        db.commit()
        gate = SettlementGateService()

        with pytest.raises(BusinessRuleError) as raised:
            gate.ensure_trading_open(db, trading_day=date(2026, 8, 6))
        gate.ensure_trading_open(db, trading_day=date(2026, 8, 7))

    assert raised.value.error_code == "TRADING_DAY_ALREADY_SETTLED"
    engine.dispose()

