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
    def list_available_details_for_update(
        db: Session,
        *,
        position_id: str,
    ) -> Sequence[PositionDetail]:
        """
        平仓下单时只锁定仍有可平数量的逐笔明细。

        SQL层直接过滤 remaining_volume - frozen_volume > 0，已关闭明细和
        已被其他订单完全冻结的明细都不会进入锁范围；排序继续使用数据库
        自增 id，保证昨/今分类内部的 FIFO 稳定。
        """

        statement = (
            select(PositionDetail)
            .where(
                PositionDetail.position_id == position_id,
                (
                    PositionDetail.remaining_volume
                    - PositionDetail.frozen_volume
                )
                > 0,
            )
            .order_by(PositionDetail.id)
            .with_for_update()
        )
        return db.scalars(statement).all()

    @staticmethod
    def list_details_by_ids_for_update(
        db: Session,
        *,
        position_id: str,
        position_detail_ids: Sequence[str],
    ) -> Sequence[PositionDetail]:
        """
        成交和撤单只锁定当前订单 Allocation 引用的持仓明细。

        position_id 条件防止异常 Allocation 跨持仓引用；固定按 id 排序，
        并与成交、撤单统一采用 Allocation 后、PositionDetail 前的锁顺序。
        """

        if not position_detail_ids:
            return []
        statement = (
            select(PositionDetail)
            .where(
                PositionDetail.position_id == position_id,
                PositionDetail.position_detail_id.in_(
                    tuple(position_detail_ids)
                ),
            )
            .order_by(PositionDetail.id)
            .with_for_update()
        )
        return db.scalars(statement).all()

    @staticmethod
    def list_open_details(
        db: Session,
        *,
        position_id: str,
    ) -> Sequence[PositionDetail]:
        """
        只读取尚有持仓的有效明细，用于成交后重算持仓汇总。

        调用方已经锁定 Position 行，因此同一持仓的其他成交或撤单事务无法
        并发修改；这里无需扩大 FOR UPDATE 范围到无关历史明细。
        """

        statement = (
            select(PositionDetail)
            .where(
                PositionDetail.position_id == position_id,
                PositionDetail.remaining_volume > 0,
            )
            .order_by(PositionDetail.id)
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
