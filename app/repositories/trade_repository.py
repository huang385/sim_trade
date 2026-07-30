from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.trade import Trade
from app.models.account import Account


class TradeRepository:
    """
    成交数据库仓储。

    Repository只负责构造SQL和把对象加入Session，不计算成交额、不修改
    账户和持仓，也不执行commit或rollback。事务统一由结算Service管理。
    """

    @staticmethod
    def get_by_trade_id(db: Session, trade_id: str) -> Trade | None:
        """根据对外成交编号查询一条成交。"""

        return db.scalar(select(Trade).where(Trade.trade_id == trade_id))

    @staticmethod
    def get_by_trade_id_for_user(
        db: Session,
        *,
        trade_id: str,
        user_id: str,
    ) -> Trade | None:
        """一次查询返回普通用户有权访问的成交，避免成交编号枚举。"""

        return db.scalar(
            select(Trade)
            .join(Account, Account.account_id == Trade.account_id)
            .where(
                Trade.trade_id == trade_id,
                Account.user_id == user_id,
            )
        )

    @staticmethod
    def get_by_order_market_event(
        db: Session,
        *,
        order_id: str,
        market_event_id: str,
    ) -> Trade | None:
        """按数据库唯一幂等键查询成交。"""

        return db.scalar(
            select(Trade).where(
                Trade.order_id == order_id,
                Trade.market_event_id == market_event_id,
            )
        )

    @staticmethod
    def list(
        db: Session,
        *,
        account_id: str | None = None,
        order_id: str | None = None,
        after_id: int | None = None,
        limit: int = 100,
    ) -> Sequence[Trade]:
        """
        按账户或订单组合查询成交。

        当前数据量较小先按自增主键稳定排序；后续增加游标分页时可以直接
        在本方法上增加last_id和limit，而不需要修改API业务逻辑。
        """

        statement = select(Trade)
        if account_id is not None:
            statement = statement.where(Trade.account_id == account_id)
        if order_id is not None:
            statement = statement.where(Trade.order_id == order_id)
        if after_id is not None:
            statement = (
                statement.where(Trade.id > after_id)
                .order_by(Trade.id)
                .limit(limit)
            )
            return db.scalars(statement).all()
        rows = db.scalars(
            statement.order_by(Trade.id.desc()).limit(limit)
        ).all()
        return list(reversed(rows))

    @staticmethod
    def list_page(
        db: Session,
        *,
        account_id: str | None,
        order_id: str | None,
        before_id: int | None,
        fetch_size: int,
    ) -> Sequence[Trade]:
        """按过滤条件和自增主键倒序读取一页成交，不使用OFFSET。"""

        statement = select(Trade)
        if account_id is not None:
            statement = statement.where(Trade.account_id == account_id)
        if order_id is not None:
            statement = statement.where(Trade.order_id == order_id)
        if before_id is not None:
            statement = statement.where(Trade.id < before_id)
        return db.scalars(
            statement.order_by(Trade.id.desc()).limit(fetch_size)
        ).all()

    @staticmethod
    def add(db: Session, trade: Trade) -> None:
        """把成交加入当前Session，是否提交由成交结算服务决定。"""

        db.add(trade)
