from typing import Sequence

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.instrument import Instrument
from app.models.position import Position
from app.models.position_detail import PositionDetail
from app.models.position_freeze_allocation import (
    PositionFreezeAllocation,
)


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
    def list_by_account_contract(
        db: Session,
        *,
        account_id: str,
        exchange_id: str,
        symbol: str,
    ) -> Sequence[Position]:
        """查询账户指定合约的多空持仓，供成交后清理实时快照使用。"""

        statement = (
            select(Position)
            .where(
                Position.account_id == account_id,
                Position.exchange_id == exchange_id,
                Position.symbol == symbol,
            )
            .order_by(Position.id)
        )
        return db.scalars(statement).all()

    @staticmethod
    def get_by_position_id(
        db: Session,
        position_id: str,
    ) -> Position | None:
        """按业务编号读取一条持仓，不加行锁。"""

        return db.scalar(
            select(Position).where(
                Position.position_id == position_id
            )
        )

    @staticmethod
    def get_by_position_id_for_user(
        db: Session,
        *,
        position_id: str,
        user_id: str,
    ) -> Position | None:
        """一次查询返回普通用户有权访问的持仓，避免持仓编号枚举。"""

        return db.scalar(
            select(Position)
            .join(Account, Account.account_id == Position.account_id)
            .where(
                Position.position_id == position_id,
                Account.user_id == user_id,
            )
        )

    @staticmethod
    def get_by_position_id_for_update(
        db: Session,
        position_id: str,
    ) -> Position | None:
        """定时持久化时按业务编号锁定一条持仓。"""

        return db.scalar(
            select(Position)
            .where(Position.position_id == position_id)
            .with_for_update()
        )

    @staticmethod
    def list_by_position_ids_for_update(
        db: Session,
        *,
        account_id: str,
        position_ids: Sequence[str],
    ) -> Sequence[Position]:
        """
        按固定数据库主键顺序批量锁定一个账户的Dirty持仓。

        account_id条件防止调用方传入跨账户编号；统一排序与成交结算使用的
        Account→Position锁顺序一致，降低并发持久化死锁风险。
        """

        if not position_ids:
            return []
        statement = (
            select(Position)
            .where(
                Position.account_id == account_id,
                Position.position_id.in_(tuple(position_ids)),
            )
            .order_by(Position.id)
            .with_for_update()
        )
        return db.scalars(statement).all()

    @staticmethod
    def list_account_ids_for_positions(
        db: Session,
        position_ids: Sequence[str],
    ) -> Sequence[tuple[str, str]]:
        """批量返回Dirty持仓与账户映射，不加锁且不修改数据。"""

        if not position_ids:
            return []
        statement = (
            select(Position.position_id, Position.account_id)
            .where(Position.position_id.in_(tuple(position_ids)))
            .order_by(Position.account_id, Position.id)
        )
        return db.execute(statement).all()

    @staticmethod
    def list_active_calculation_rows(db: Session):
        """
        一次读取全部活动持仓计算行，用于短周期内存缓存刷新。

        只返回total_volume和remaining_volume均大于0的数据；调用方立即转换
        为不可变快照，禁止把这些ORM对象跨Session保存。
        """

        statement = (
            select(Position, PositionDetail, Instrument, Account)
            .join(
                PositionDetail,
                PositionDetail.position_id == Position.position_id,
            )
            .join(
                Instrument,
                Instrument.order_book_id == Position.order_book_id,
            )
            .join(
                Account,
                Account.account_id == Position.account_id,
            )
            .where(
                Position.total_volume > 0,
                PositionDetail.remaining_volume > 0,
            )
            .order_by(Position.id, PositionDetail.id)
        )
        return db.execute(statement).all()

    @staticmethod
    def list_active_calculation_rows_by_contracts(
        db: Session,
        contract_keys: Sequence[tuple[str, str]],
    ):
        """
        一次批量读取指定合约的活动持仓计算行。

        成交事实只会刷新受影响合约，禁止按合约循环执行SQL。交易所和合约
        代码在进入查询前统一标准化，返回结构与全量查询保持一致。
        """

        keys = tuple(
            sorted(
                {
                    (
                        exchange_id.strip().upper(),
                        symbol.strip().upper(),
                    )
                    for exchange_id, symbol in contract_keys
                }
            )
        )
        if not keys:
            return []
        statement = (
            select(Position, PositionDetail, Instrument, Account)
            .join(
                PositionDetail,
                PositionDetail.position_id == Position.position_id,
            )
            .join(
                Instrument,
                Instrument.order_book_id == Position.order_book_id,
            )
            .join(
                Account,
                Account.account_id == Position.account_id,
            )
            .where(
                Position.total_volume > 0,
                PositionDetail.remaining_volume > 0,
                tuple_(
                    Position.exchange_id,
                    Position.symbol,
                ).in_(keys),
            )
            .order_by(Position.id, PositionDetail.id)
        )
        return db.execute(statement).all()

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
    def list_details_by_order_for_update(
        db: Session,
        *,
        position_id: str,
        order_id: str,
    ) -> Sequence[PositionDetail]:
        """
        先于冻结分配记录锁定当前平仓订单引用的持仓明细。

        通过 Allocation 仅筛选目标明细，并使用 ``FOR UPDATE OF
        position_detail`` 将行锁限制在持仓明细表。这样既不会锁住同一
        Position 下与当前订单无关的历史明细，也能让撤单事务遵循
        Order→Account→Position→PositionDetail→Allocation 的统一锁顺序。
        """

        statement = (
            select(PositionDetail)
            .join(
                PositionFreezeAllocation,
                PositionFreezeAllocation.position_detail_id
                == PositionDetail.position_detail_id,
            )
            .where(
                PositionDetail.position_id == position_id,
                PositionFreezeAllocation.position_id == position_id,
                PositionFreezeAllocation.order_id == order_id,
            )
            .order_by(PositionDetail.id)
            .with_for_update(of=PositionDetail)
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
    def list_open_details_for_update(
        db: Session,
        *,
        position_id: str,
    ) -> Sequence[PositionDetail]:
        """定时持久化时按固定顺序锁定仍有数量的逐笔明细。"""

        statement = (
            select(PositionDetail)
            .where(
                PositionDetail.position_id == position_id,
                PositionDetail.remaining_volume > 0,
            )
            .order_by(PositionDetail.id)
            .with_for_update()
        )
        return db.scalars(statement).all()

    @staticmethod
    def list_open_details_by_position_ids_for_update(
        db: Session,
        *,
        position_ids: Sequence[str],
    ) -> Sequence[PositionDetail]:
        """按持仓编号和数据库主键稳定排序，批量锁定全部有效持仓明细。"""

        if not position_ids:
            return []
        statement = (
            select(PositionDetail)
            .where(
                PositionDetail.position_id.in_(tuple(position_ids)),
                PositionDetail.remaining_volume > 0,
            )
            .order_by(PositionDetail.position_id, PositionDetail.id)
            .with_for_update()
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
