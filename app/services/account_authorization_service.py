from typing import Sequence

from sqlalchemy.orm import Session

from app.common.exceptions import AuthorizationError, ResourceNotFoundError
from app.enums.auth_enums import UserRole
from app.models.account import Account
from app.models.app_user import AppUser
from app.repositories.account_repository import AccountRepository


class AccountAuthorizationService:
    """集中实现管理员全局访问和普通用户账户归属规则。"""

    def __init__(
        self, repository: AccountRepository | None = None
    ):
        self.repository = repository or AccountRepository()

    def require_account_access(
        self,
        db: Session,
        current_user: AppUser,
        account_id: str,
        *,
        for_update: bool = False,
    ) -> Account:
        normalized = account_id.strip()
        if current_user.role == UserRole.ADMIN.value:
            account = (
                self.repository.get_by_account_id_for_update(db, normalized)
                if for_update
                else self.repository.get_by_account_id(db, normalized)
            )
            if account is None:
                raise ResourceNotFoundError(
                    "交易账户不存在",
                    error_code="ACCOUNT_NOT_FOUND",
                )
            return account

        account = self.repository.get_owned_account(
            db,
            account_id=normalized,
            user_id=current_user.user_id,
            for_update=for_update,
        )
        if account is None:
            raise AuthorizationError(
                "无权访问该交易账户",
                error_code="ACCOUNT_ACCESS_DENIED",
            )
        return account

    def list_accessible_accounts(
        self,
        db: Session,
        current_user: AppUser,
    ) -> Sequence[Account]:
        if current_user.role == UserRole.ADMIN.value:
            return self.repository.list_all(db)
        return self.repository.list_by_user_id(db, current_user.user_id)
