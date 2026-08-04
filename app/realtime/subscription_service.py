from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.common.exceptions import AuthorizationError, BusinessValidationError
from app.core.config import settings
from app.enums.auth_enums import UserRole, UserStatus
from app.repositories.account_repository import AccountRepository
from app.repositories.user_repository import UserRepository


@dataclass(frozen=True)
class RealtimeUserIdentity:
    """跨Session传递的不可变身份标量，不保存ORM用户对象。"""

    user_id: str
    role: str


@dataclass(frozen=True)
class SubscriptionAuthorization:
    """一次数据库实时授权检查的不可变结果。"""

    identity: RealtimeUserIdentity
    account_ids: frozenset[str]


class SubscriptionService:
    """批量校验交易账户存在性、归属和单连接订阅上限。"""

    def __init__(
        self,
        repository: AccountRepository | None = None,
        user_repository: UserRepository | None = None,
        *,
        max_subscriptions: int | None = None,
    ):
        self.repository = repository or AccountRepository()
        self.user_repository = user_repository or UserRepository()
        self.max_subscriptions = (
            max_subscriptions
            if max_subscriptions is not None
            else settings.ws_max_subscriptions_per_connection
        )

    def authorize(
        self,
        db: Session,
        *,
        identity: RealtimeUserIdentity,
        requested_account_ids: list[str],
        existing_account_ids: set[str],
    ) -> frozenset[str]:
        """一次IN查询校验全部目标；混入任一未授权账户则整体拒绝。"""

        requested = list(dict.fromkeys(requested_account_ids))
        combined = existing_account_ids | set(requested)
        if len(combined) > self.max_subscriptions:
            raise BusinessValidationError(
                "单连接订阅账户数量超过限制",
                error_code="WS_SUBSCRIPTION_LIMIT_EXCEEDED",
            )
        accounts = list(self.repository.list_by_account_ids(db, requested))
        if len(accounts) != len(requested):
            raise AuthorizationError(
                "无权订阅目标交易账户",
                error_code="WS_ACCOUNT_ACCESS_DENIED",
            )
        by_id = {account.account_id: account for account in accounts}
        if set(by_id) != set(requested):
            raise AuthorizationError(
                "无权订阅目标交易账户",
                error_code="WS_ACCOUNT_ACCESS_DENIED",
            )
        if identity.role != UserRole.ADMIN.value and any(
            account.user_id != identity.user_id for account in accounts
        ):
            raise AuthorizationError(
                "无权订阅目标交易账户",
                error_code="WS_ACCOUNT_ACCESS_DENIED",
            )
        return frozenset(requested)

    def authorize_current(
        self,
        db: Session,
        *,
        user_id: str,
        requested_account_ids: list[str],
        existing_account_ids: set[str],
    ) -> SubscriptionAuthorization:
        """重新读取当前用户角色，并在SQL中批量校验最新账户归属。"""

        user = self.user_repository.get_by_user_id(db, user_id)
        if user is None or user.status != UserStatus.ACTIVE.value:
            raise AuthorizationError(
                "当前用户不可用",
                error_code="WS_USER_INACTIVE",
            )
        requested = list(dict.fromkeys(requested_account_ids))
        if len(existing_account_ids | set(requested)) > self.max_subscriptions:
            raise BusinessValidationError(
                "单连接订阅账户数量超过限制",
                error_code="WS_SUBSCRIPTION_LIMIT_EXCEEDED",
            )
        if user.role == UserRole.ADMIN.value:
            accounts = list(
                self.repository.list_by_account_ids(db, requested)
            )
        else:
            # 普通用户的所有权限制下推到同一条IN查询，既避免逐账户SQL，
            # 也不会先读取其他用户账户再在Python中判断。
            accounts = list(
                self.repository.list_owned_by_account_ids(
                    db,
                    account_ids=requested,
                    user_id=user.user_id,
                )
            )
        authorized = frozenset(account.account_id for account in accounts)
        if authorized != frozenset(requested):
            raise AuthorizationError(
                "无权订阅目标交易账户",
                error_code="WS_ACCOUNT_ACCESS_DENIED",
            )
        return SubscriptionAuthorization(
            identity=RealtimeUserIdentity(
                user_id=user.user_id,
                role=user.role,
            ),
            account_ids=authorized,
        )

    def recheck_current_subscriptions(
        self,
        db: Session,
        *,
        user_id: str,
        subscribed_account_ids: set[str],
    ) -> SubscriptionAuthorization:
        """定期复查角色和全部现有订阅；返回仍然授权的账户集合。"""

        user = self.user_repository.get_by_user_id(db, user_id)
        if user is None or user.status != UserStatus.ACTIVE.value:
            raise AuthorizationError(
                "当前用户不可用",
                error_code="WS_USER_INACTIVE",
            )
        requested = sorted(subscribed_account_ids)
        if user.role == UserRole.ADMIN.value:
            accounts = self.repository.list_by_account_ids(db, requested)
        else:
            accounts = self.repository.list_owned_by_account_ids(
                db,
                account_ids=requested,
                user_id=user.user_id,
            )
        return SubscriptionAuthorization(
            identity=RealtimeUserIdentity(user.user_id, user.role),
            account_ids=frozenset(
                account.account_id for account in accounts
            ),
        )
