from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.common.exceptions import (
    DataAccessError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from app.common.time_utils import utc_now
from app.enums.auth_enums import UserStatus
from app.models.app_user import AppUser
from app.repositories.user_repository import UserRepository
from app.schemas.user_schema import UserCreateRequest
from app.services.password_service import PasswordService


class AdminUserService:
    """管理员用户创建、查询、禁用和解锁事务。"""

    def __init__(
        self,
        *,
        repository: UserRepository,
        password_service: PasswordService,
    ):
        self.repository = repository
        self.password_service = password_service

    def create_user(
        self, db: Session, request: UserCreateRequest
    ) -> AppUser:
        if self.repository.get_by_user_id(db, request.user_id):
            raise ResourceConflictError(
                "用户编号已存在", error_code="USER_ID_EXISTS"
            )
        if self.repository.get_by_username(db, request.username):
            raise ResourceConflictError(
                "用户名已存在", error_code="USERNAME_EXISTS"
            )
        now = utc_now()
        user = AppUser(
            user_id=request.user_id,
            username=request.username,
            password_hash=self.password_service.hash_password(
                request.password
            ),
            display_name=request.display_name,
            role=request.role.value,
            status=UserStatus.ACTIVE.value,
            password_changed_at=now,
        )
        self.repository.add(db, user)
        try:
            db.commit()
            db.refresh(user)
            return user
        except IntegrityError as exc:
            db.rollback()
            raise ResourceConflictError(
                "用户编号或用户名已存在"
            ) from exc
        except SQLAlchemyError as exc:
            db.rollback()
            raise DataAccessError("创建用户失败") from exc

    def list_users(self, db: Session):
        return self.repository.list_all(db)

    def update_status(
        self,
        db: Session,
        *,
        user_id: str,
        status: UserStatus,
    ) -> AppUser:
        user = self.repository.get_by_user_id_for_update(
            db, user_id.strip()
        )
        if user is None:
            raise ResourceNotFoundError(
                "用户不存在", error_code="USER_NOT_FOUND"
            )
        user.status = status.value
        if status == UserStatus.ACTIVE:
            user.failed_login_count = 0
            user.locked_until = None
        elif status == UserStatus.DISABLED:
            user.locked_until = None
        try:
            db.commit()
            db.refresh(user)
            return user
        except SQLAlchemyError as exc:
            db.rollback()
            raise DataAccessError("更新用户状态失败") from exc
