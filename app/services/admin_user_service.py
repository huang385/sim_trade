from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.common.exceptions import (
    BusinessValidationError,
    DataAccessError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from app.common.time_utils import utc_now
from app.enums.auth_enums import UserStatus
from app.models.app_user import AppUser
from app.repositories.auth_refresh_session_repository import (
    AuthRefreshSessionRepository,
)
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
        refresh_repository: AuthRefreshSessionRepository | None = None,
    ):
        self.repository = repository
        self.password_service = password_service
        self.refresh_repository = (
            refresh_repository or AuthRefreshSessionRepository()
        )

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

    def change_password(
        self,
        db: Session,
        *,
        user_id: str,
        new_password: str,
    ) -> AppUser:
        """
        锁定数据库用户并真实更新密码哈希。

        本阶段只提供管理员Service能力，不开放匿名或公共改密接口。事务提交
        和回滚始终由Service负责，Repository只执行查询。
        """

        if len(new_password) < 12:
            raise BusinessValidationError(
                "密码至少需要12个字符",
                error_code="PASSWORD_TOO_SHORT",
            )
        user = self.repository.get_by_user_id_for_update(
            db,
            user_id.strip(),
        )
        if user is None:
            raise ResourceNotFoundError(
                "用户不存在",
                error_code="USER_NOT_FOUND",
            )
        changed_at = utc_now()
        user.password_hash = self.password_service.hash_password(new_password)
        user.password_changed_at = changed_at
        try:
            # 密码更新和Refresh会话撤销属于同一数据库事务，任何一步失败
            # 都会全部回滚。既有Access Token不加入黑名单，仍最多存活到
            # 原15分钟有效期结束。
            self.refresh_repository.revoke_active_by_user_id(
                db,
                user_id=user.user_id,
                revoked_at=changed_at,
            )
            db.commit()
            db.refresh(user)
            return user
        except SQLAlchemyError as exc:
            db.rollback()
            raise DataAccessError("修改用户密码失败") from exc

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
        try:
            user.status = status.value
            if status == UserStatus.ACTIVE:
                user.failed_login_count = 0
                user.locked_until = None
            elif status == UserStatus.DISABLED:
                user.locked_until = None
                # 禁用和Refresh会话撤销必须原子提交。之后即使重新启用用户，
                # 已写入revoked_at的旧会话也不会恢复。
                self.refresh_repository.revoke_active_by_user_id(
                    db,
                    user_id=user.user_id,
                    revoked_at=utc_now(),
                )
            db.commit()
            db.refresh(user)
            return user
        except SQLAlchemyError as exc:
            db.rollback()
            raise DataAccessError("更新用户状态失败") from exc
