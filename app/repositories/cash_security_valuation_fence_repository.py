from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.time_utils import utc_now
from app.models.cash_security_valuation_writer_fence import (
    CashSecurityValuationWriterFence,
)


class CashSecurityValuationFenceRepository:
    """Repository methods deliberately leave transaction control to callers."""

    FENCE_NAME = "CASH_SECURITY_VALUATION"

    def activate(self, db: Session, *, owner: str, fencing_token: str) -> bool:
        token = int(fencing_token)
        row = db.scalar(
            select(CashSecurityValuationWriterFence)
            .where(CashSecurityValuationWriterFence.fence_name == self.FENCE_NAME)
            .with_for_update()
        )
        if row is None:
            db.add(CashSecurityValuationWriterFence(
                fence_name=self.FENCE_NAME,
                fencing_token=token,
                owner=owner,
                updated_at=utc_now(),
            ))
            return True
        if row.fencing_token > token:
            return False
        row.fencing_token = token
        row.owner = owner
        row.updated_at = utc_now()
        return True

    def is_current(self, db: Session, *, owner: str, fencing_token: str) -> bool:
        row = db.scalar(
            select(CashSecurityValuationWriterFence)
            .where(CashSecurityValuationWriterFence.fence_name == self.FENCE_NAME)
            .with_for_update()
        )
        return bool(
            row is not None
            and row.owner == owner
            and row.fencing_token == int(fencing_token)
        )
