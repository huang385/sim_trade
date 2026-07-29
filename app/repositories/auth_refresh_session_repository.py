from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.auth_refresh_session import AuthRefreshSession


class AuthRefreshSessionRepository:
    """Refresh会话数据库访问，不保存原始Token。"""

    @staticmethod
    def add(db: Session, session: AuthRefreshSession) -> None:
        db.add(session)

    @staticmethod
    def get_by_jti_for_update(
        db: Session, jti: str
    ) -> AuthRefreshSession | None:
        return db.scalar(
            select(AuthRefreshSession)
            .where(AuthRefreshSession.jti == jti)
            .with_for_update()
        )
