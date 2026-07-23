from datetime import date, datetime
from decimal import Decimal
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.order import Order
from app.enums.order_enums import OffsetFlag, OrderStatus, OrderType


class OrderRepository:
    """
    订单数据库仓储。

    Repository 只负责查询和构造数据库对象：
    1. 不计算保证金和手续费；
    2. 不检查账户资金；
    3. 不执行 commit 或 rollback；
    4. 不抛出 HTTPException。

    事务统一由 OrderService 管理。
    """

    @staticmethod
    def get_by_order_id(
        db: Session,
        order_id: str,
    ) -> Order | None:
        """根据系统订单编号查询单笔订单。"""

        statement = select(Order).where(Order.order_id == order_id)
        return db.scalar(statement)

    @staticmethod
    def get_by_order_id_for_update(
        db: Session,
        order_id: str,
    ) -> Order | None:
        """锁定订单行，防止同一订单被并发 Tick 重复结算。"""

        statement = (
            select(Order)
            .where(Order.order_id == order_id)
            .with_for_update()
        )
        return db.scalar(statement)

    @staticmethod
    def get_by_client_order_id(
        db: Session,
        account_id: str,
        client_order_id: str,
    ) -> Order | None:
        """根据账户和客户端订单编号查询，用于幂等判断。"""

        statement = select(Order).where(
            Order.account_id == account_id,
            Order.client_order_id == client_order_id,
        )
        return db.scalar(statement)

    @staticmethod
    def list_by_account(
        db: Session,
        account_id: str,
    ) -> Sequence[Order]:
        """按数据库写入顺序查询指定账户的全部订单。"""

        statement = (
            select(Order)
            .where(Order.account_id == account_id)
            .order_by(Order.id)
        )
        return db.scalars(statement).all()

    @staticmethod
    def list_active_after_id(
        db: Session,
        *,
        last_id: int,
        batch_size: int,
    ) -> Sequence[Order]:
        """
        使用自增主键游标分页读取等待撮合的活动订单。

        不使用大OFFSET，也不执行commit或rollback。返回结果只包含状态、
        剩余量、订单类型和开平标志均符合活动订单条件的数据库最新记录。
        """

        statement = (
            select(Order)
            .where(
                Order.id > last_id,
                Order.status.in_(
                    (
                        OrderStatus.ACCEPTED.value,
                        OrderStatus.PARTIALLY_FILLED.value,
                    )
                ),
                Order.remaining_volume > 0,
                Order.order_type == OrderType.LIMIT.value,
                Order.offset_flag == OffsetFlag.OPEN.value,
            )
            .order_by(Order.id)
            .limit(batch_size)
        )
        return db.scalars(statement).all()

    @staticmethod
    def create(
        db: Session,
        *,
        order_id: str,
        client_order_id: str,
        account_id: str,
        order_book_id: str,
        symbol: str,
        exchange_id: str,
        trading_day: date,
        direction: str,
        offset_flag: str,
        order_type: str,
        limit_price: Decimal,
        total_volume: int,
        status: str,
        submit_status: str,
        frozen_margin: Decimal,
        frozen_commission: Decimal,
        created_at: datetime,
        accepted_at: datetime,
    ) -> Order:
        """
        构造并加入一笔已接受订单。

        此方法只调用 db.add，不刷新对象也不提交事务。
        如果后续账户更新或订单写入失败，OrderService 可以统一回滚。
        """

        order = Order(
            order_id=order_id,
            client_order_id=client_order_id,
            account_id=account_id,
            order_book_id=order_book_id,
            symbol=symbol,
            exchange_id=exchange_id,
            trading_day=trading_day,
            direction=direction,
            offset_flag=offset_flag,
            order_type=order_type,
            limit_price=limit_price,
            total_volume=total_volume,
            # 当前阶段不做撮合，成交数量和平均价保持初始值。
            traded_volume=0,
            remaining_volume=total_volume,
            # 当前阶段不实现撤单，新订单撤销量固定为0。
            cancelled_volume=0,
            average_price=None,
            status=status,
            submit_status=submit_status,
            frozen_margin=frozen_margin,
            frozen_commission=frozen_commission,
            # 开仓订单不冻结已有持仓。
            frozen_position_volume=0,
            reject_code=None,
            reject_message=None,
            created_at=created_at,
            accepted_at=accepted_at,
            updated_at=accepted_at,
        )

        # 只加入当前 Session，是否提交由上层事务决定。
        db.add(order)
        return order
