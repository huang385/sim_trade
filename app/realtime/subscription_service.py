from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.common.exceptions import AuthorizationError, BusinessValidationError
from app.core.config import settings
from app.enums.auth_enums import UserRole
from app.repositories.account_repository import AccountRepository


@dataclass(frozen=True)
class RealtimeUserIdentity:
    """跨Session传递的不可变身份标量，不保存ORM用户对象。"""

    user_id: str
    role: str


class SubscriptionService:
    """批量校验交易账户存在性、归属和单连接订阅上限。"""

    def __init__(
        self,
        repository: AccountRepository | None = None,
        *,
        max_subscriptions: int | None = None,
    ):
        self.repository = repository or AccountRepository()
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
