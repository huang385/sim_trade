"""风险模块只读查询入口。"""

from sqlalchemy.orm import Session

from app.repositories.risk_repository import RiskRepository


class RiskQueryService:
    def __init__(self, repository: RiskRepository | None = None) -> None:
        self.repository = repository or RiskRepository()

    def latest_task(self, db: Session, account_id: str):
        rows = self.repository.list_tasks_by_account(db, account_id, limit=1)
        return rows[0] if rows else None

    def list_events(self, db: Session, account_id: str, *, limit: int):
        return self.repository.list_events_by_account(
            db, account_id, limit=limit
        )

    def list_tasks(self, db: Session, account_id: str, *, limit: int):
        return self.repository.list_tasks_by_account(
            db, account_id, limit=limit
        )


_risk_query_service = RiskQueryService()


def get_risk_query_service() -> RiskQueryService:
    return _risk_query_service
