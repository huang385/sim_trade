from typing import Sequence

from sqlalchemy.orm import Session

from app.common.exceptions import AuthorizationError, ResourceNotFoundError
from app.enums.auth_enums import UserRole
from app.models.account import Account
from app.models.app_user import AppUser
from app.repositories.account_repository import AccountRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.position_repository import PositionRepository
from app.repositories.trade_repository import TradeRepository


class AccountAuthorizationService:
    """集中实现管理员全局访问和普通用户账户归属规则。"""

    def __init__(
        self,
        repository: AccountRepository | None = None,
        order_repository: OrderRepository | None = None,
        trade_repository: TradeRepository | None = None,
        position_repository: PositionRepository | None = None,
    ):
        self.repository = repository or AccountRepository()
        self.order_repository = order_repository or OrderRepository()
        self.trade_repository = trade_repository or TradeRepository()
        self.position_repository = (
            position_repository or PositionRepository()
        )

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

    @staticmethod
    def require_loaded_account_access(
        current_user: AppUser,
        account: Account,
        *,
        conceal_forbidden: bool = False,
    ) -> Account:
        """
        校验已经由业务事务加载或锁定的账户，不再执行SQL。

        下单和撤单使用该入口复用同一个``SELECT FOR UPDATE``对象。资源ID
        查询设置``conceal_forbidden``时，普通用户访问他人资源与资源不存在
        使用相同404响应，避免泄露资源是否真实存在。
        """

        if current_user.role == UserRole.ADMIN.value:
            return account
        if account.user_id == current_user.user_id:
            return account
        if conceal_forbidden:
            raise ResourceNotFoundError(
                "目标资源不存在",
                error_code="RESOURCE_NOT_FOUND",
            )
        raise AuthorizationError(
            "无权访问该交易账户",
            error_code="ACCOUNT_ACCESS_DENIED",
        )

    def list_accessible_accounts(
        self,
        db: Session,
        current_user: AppUser,
    ) -> Sequence[Account]:
        if current_user.role == UserRole.ADMIN.value:
            return self.repository.list_all(db)
        return self.repository.list_by_user_id(db, current_user.user_id)

    @staticmethod
    def _resource_not_found() -> ResourceNotFoundError:
        """普通用户对不存在和无权资源始终得到相同安全响应。"""

        return ResourceNotFoundError(
            "目标资源不存在",
            error_code="RESOURCE_NOT_FOUND",
        )

    def require_order_access(
        self,
        db: Session,
        current_user: AppUser,
        order_id: str,
    ):
        """按用户范围加载订单，避免先查询资源再查询账户。"""

        normalized = order_id.strip()
        order = (
            self.order_repository.get_by_order_id(db, normalized)
            if current_user.role == UserRole.ADMIN.value
            else self.order_repository.get_by_order_id_for_user(
                db,
                order_id=normalized,
                user_id=current_user.user_id,
            )
        )
        if order is None:
            raise self._resource_not_found()
        return order

    def require_trade_access(
        self,
        db: Session,
        current_user: AppUser,
        trade_id: str,
    ):
        """按用户范围加载成交，不暴露其他用户成交是否存在。"""

        normalized = trade_id.strip()
        trade = (
            self.trade_repository.get_by_trade_id(db, normalized)
            if current_user.role == UserRole.ADMIN.value
            else self.trade_repository.get_by_trade_id_for_user(
                db,
                trade_id=normalized,
                user_id=current_user.user_id,
            )
        )
        if trade is None:
            raise self._resource_not_found()
        return trade

    def require_position_access(
        self,
        db: Session,
        current_user: AppUser,
        position_id: str,
    ):
        """按用户范围加载持仓，不暴露其他用户持仓是否存在。"""

        normalized = position_id.strip()
        position = (
            self.position_repository.get_by_position_id(db, normalized)
            if current_user.role == UserRole.ADMIN.value
            else self.position_repository.get_by_position_id_for_user(
                db,
                position_id=normalized,
                user_id=current_user.user_id,
            )
        )
        if position is None:
            raise self._resource_not_found()
        return position
