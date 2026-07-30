from datetime import datetime

from sqlalchemy import select, update
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

    @staticmethod
    def revoke_active_by_user_id(
        db: Session,
        *,
        user_id: str,
        revoked_at: datetime,
    ) -> int:
        """
        批量撤销用户尚未撤销的Refresh会话。

        只更新服务端会话状态，不读取原始Token或Token哈希；事务提交和
        回滚由调用Service统一负责。
        """

        result = db.execute(
            update(AuthRefreshSession)
            .where(
                AuthRefreshSession.user_id == user_id,
                AuthRefreshSession.revoked_at.is_(None),
            )
            .values(revoked_at=revoked_at)
        )
        return result.rowcount
