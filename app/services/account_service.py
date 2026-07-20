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
from app.enums.account_enums import AccountStatus
from app.models.account import Account
from app.repositories.account_repository import AccountRepository
from app.schemas.account_schema import AccountCreate


class AccountService:
    """
    模拟账户业务服务。
    """

    def __init__(
        self,
        repository: AccountRepository,
    ):
        self.repository = repository

    def create_account(
        self,
        db: Session,
        request: AccountCreate,
    ) -> Account:
        """
        创建模拟账户。
        """

        exists = self.repository.get_by_account_id(
            db=db,
            account_id=request.account_id,
        )

        if exists is not None:
            raise ResourceConflictError(
                "账户已经存在"
            )

        account = Account(
            account_id=request.account_id,
            user_id=request.user_id,
            account_name=request.account_name,
            account_type=request.account_type.value,

            initial_cash=request.initial_cash,
            cash_balance=request.initial_cash,
            available_cash=request.initial_cash,
            frozen_cash=0,

            equity=request.initial_cash,

            used_margin=0,
            frozen_margin=0,

            realized_pnl=0,
            unrealized_pnl=0,
            daily_pnl=0,

            used_commission=0,
            frozen_commission=0,

            risk_ratio=0,
            status=AccountStatus.NORMAL.value,
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
    )
