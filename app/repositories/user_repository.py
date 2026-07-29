from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.app_user import AppUser


class UserRepository:
    """平台用户数据库访问；事务提交和回滚由Service负责。"""

    @staticmethod
    def add(db: Session, user: AppUser) -> None:
        db.add(user)

    @staticmethod
    def get_by_user_id(db: Session, user_id: str) -> AppUser | None:
        return db.scalar(
            select(AppUser).where(AppUser.user_id == user_id)
        )

    @staticmethod
    def get_by_username(db: Session, username: str) -> AppUser | None:
        return db.scalar(
            select(AppUser).where(AppUser.username == username)
        )

    @staticmethod
    def get_by_username_for_update(
        db: Session, username: str
    ) -> AppUser | None:
        return db.scalar(
            select(AppUser)
            .where(AppUser.username == username)
            .with_for_update()
        )

    @staticmethod
    def get_by_user_id_for_update(
        db: Session, user_id: str
    ) -> AppUser | None:
        return db.scalar(
            select(AppUser)
            .where(AppUser.user_id == user_id)
            .with_for_update()
        )

    @staticmethod
    def list_all(db: Session) -> Sequence[AppUser]:
        return db.scalars(select(AppUser).order_by(AppUser.id)).all()
