from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.position import Position
from app.models.position_detail import PositionDetail


class PositionRepository:
    """
    持仓汇总和逐笔明细数据库仓储。

    本类不计算平均开仓价、持仓成本和保证金，只提供行锁查询及对象写入。
    这样并发控制和资金计算仍集中在TradeSettlementService中。
    """

    @staticmethod
    def get_for_update(
        db: Session,
        *,
        account_id: str,
        exchange_id: str,
        symbol: str,
        direction: str,
    ) -> Position | None:
        """
        查询并锁定指定方向的持仓汇总。

        行锁会持续到当前成交事务提交或回滚，防止两个并发成交同时读取相同
        的旧持仓数量并相互覆盖。账户行锁还会进一步串行化同账户成交。
        """

        statement = (
            select(Position)
            .where(
                Position.account_id == account_id,
                Position.exchange_id == exchange_id,
                Position.symbol == symbol,
                Position.direction == direction,
            )
            .with_for_update()
        )
        return db.scalar(statement)

    @staticmethod
    def list_by_account(db: Session, account_id: str) -> Sequence[Position]:
        """按数据库写入顺序查询账户全部多空持仓。"""

        statement = (
            select(Position)
            .where(Position.account_id == account_id)
            .order_by(Position.id)
        )
        return db.scalars(statement).all()

    @staticmethod
    def add(db: Session, position: Position) -> None:
        """加入一条新持仓汇总，不在Repository中提交。"""

        db.add(position)

    @staticmethod
    def add_detail(db: Session, detail: PositionDetail) -> None:
        """加入一条逐笔持仓明细，不在Repository中提交。"""

        db.add(detail)
