from typing import Sequence

from sqlalchemy.exc import (
    IntegrityError,
    SQLAlchemyError,
)
from sqlalchemy.orm import Session

from app.common.exceptions import (
    DataAccessError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from app.enums.account_enums import AccountRiskState, AccountStatus
from app.models.account import Account
from app.repositories.account_repository import AccountRepository
from app.schemas.account_schema import AccountCreate
from app.repositories.user_repository import UserRepository
from app.services.trading_day_service import (
    TradingDayService,
    get_trading_day_service,
)


class AccountService:
    """
    模拟账户业务服务。
    """

    def __init__(
        self,
        repository: AccountRepository,
        user_repository: UserRepository | None = None,
        trading_day_service: TradingDayService | None = None,
    ):
        self.repository = repository
        self.user_repository = user_repository or UserRepository()
        self.trading_day_service = trading_day_service or get_trading_day_service()

    def create_account(
        self,
        db: Session,
        request: AccountCreate,
    ) -> Account:
        """
        创建模拟账户。
        """

        owner = self.user_repository.get_by_user_id(db, request.user_id)
        if owner is None:
            raise ResourceNotFoundError(
                "所属用户不存在",
                error_code="ACCOUNT_OWNER_NOT_FOUND",
            )

        exists = self.repository.get_by_account_id(
            db=db,
            account_id=request.account_id,
        )

        if exists is not None:
            raise ResourceConflictError(
                "账户已经存在"
            )

        account_trading_days = tuple(
            self.repository.list_distinct_trading_days(db)
        )
        if len(account_trading_days) > 1:
            raise DataAccessError("现有账户交易日不一致，禁止创建新账户")
        trading_day = (
            account_trading_days[0]
            if account_trading_days
            else self.trading_day_service.resolve_for_account_creation(
                db,
                account_type=request.account_type.value,
            )
        )

        account = Account(
            account_id=request.account_id,
            user_id=request.user_id,
            account_name=request.account_name,
            account_type=request.account_type.value,
            option_trading_enabled=request.option_trading_enabled,

            initial_cash=request.initial_cash,
            cash_balance=request.initial_cash,
            available_cash=request.initial_cash,
            frozen_cash=0,

            equity=request.initial_cash,

            used_margin=0,
            frozen_margin=0,
            option_used_margin=0,
            option_realtime_required_margin=0,
            long_option_market_value=0,
            short_option_market_value=0,
            net_option_market_value=0,
            risk_available_cash=request.initial_cash,
            risk_state=AccountRiskState.NORMAL.value,

            realized_pnl=0,
            unrealized_pnl=0,
            daily_pnl=0,

            used_commission=0,
            frozen_commission=0,

            risk_ratio=0,
            status=AccountStatus.NORMAL.value,
            trading_day=trading_day,
        )

        try:
            self.repository.add(
                db=db,
                account=account,
            )

            db.commit()
            db.refresh(account)

            return account

        except IntegrityError as exc:
            db.rollback()

            raise ResourceConflictError(
                "账户编号已经存在"
            ) from exc

        except SQLAlchemyError as exc:
            db.rollback()

            raise DataAccessError(
                "创建账户失败"
            ) from exc

    def get_account(
        self,
        db: Session,
        account_id: str,
    ) -> Account:
        account_id = account_id.strip()

        account = self.repository.get_by_account_id(
            db=db,
            account_id=account_id,
        )

        if account is None:
            raise ResourceNotFoundError(
                "账户不存在"
            )

        return account

    def list_accounts(
        self,
        db: Session,
    ) -> Sequence[Account]:
        return self.repository.list_all(db)


def get_account_service() -> AccountService:
    """
    FastAPI依赖注入函数。
    """

    return AccountService(
        repository=AccountRepository(),
        user_repository=UserRepository(),
    )
