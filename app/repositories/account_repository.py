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
    def get_owned_account_for_update(
        db: Session,
        *,
        account_id: str,
        user_id: str,
    ) -> Account | None:
        """
        按账户编号和用户归属同时查询并锁定账户。

        普通用户交易请求必须使用该方法，把授权边界下推到SQL，而不是
        先锁定任意账户后再在Python中判断归属。不存在和属于其他用户
        都返回None，因此未授权请求不会等待或占用他人账户行锁。
        """

        statement = (
            select(Account)
            .where(
                Account.account_id == account_id,
                Account.user_id == user_id,
            )
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
    def list_by_user_id(
        db: Session,
        user_id: str,
    ) -> Sequence[Account]:
        """一次查询返回普通用户拥有的全部交易账户。"""

        return db.scalars(
            select(Account)
            .where(Account.user_id == user_id)
            .order_by(Account.id)
        ).all()

    @staticmethod
    def get_owned_account(
        db: Session,
        *,
        account_id: str,
        user_id: str,
        for_update: bool = False,
    ) -> Account | None:
        """把账户存在性和归属校验合并为一次数据库查询。"""

        statement = select(Account).where(
            Account.account_id == account_id,
            Account.user_id == user_id,
        )
        if for_update:
            statement = statement.with_for_update()
        return db.scalar(statement)

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
