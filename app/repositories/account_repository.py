from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.account import Account


class AccountRepository:
    """
    账户数据库仓储。

    只负责数据库查询和写入，
    不负责commit和业务判断。
    """

    @staticmethod
    def add(
        db: Session,
        account: Account,
    ) -> None:
        db.add(account)

    @staticmethod
    def get_by_account_id(
        db: Session,
        account_id: str,
    ) -> Account | None:
        statement = select(Account).where(
            Account.account_id == account_id
        )

        return db.scalar(statement)

    @staticmethod
    def get_by_account_id_for_update(
        db: Session,
        account_id: str,
    ) -> Account | None:
        """
        查询并锁定账户记录。

        SELECT FOR UPDATE 会一直持有账户行锁直到当前事务提交或回滚。
        同一账户的并发下单请求因此会按顺序检查和冻结资金，
        不会同时读取到同一份 available_cash。
        """

        # 行锁必须与冻结资金和创建订单处于同一个事务中。
        statement = (
            select(Account)
            .where(Account.account_id == account_id)
            .with_for_update()
        )

        return db.scalar(statement)

    @staticmethod
    def list_all(
        db: Session,
    ) -> Sequence[Account]:
        statement = select(Account).order_by(
            Account.id
        )

        return db.scalars(statement).all()

    @staticmethod
    def list_by_account_ids(
        db: Session,
        account_ids: Sequence[str],
    ) -> Sequence[Account]:
        """批量读取指定账户，供实时盈亏周期快照补齐已全平账户。"""

        if not account_ids:
            return []
        statement = (
            select(Account)
            .where(Account.account_id.in_(tuple(account_ids)))
            .order_by(Account.id)
        )
        return db.scalars(statement).all()
