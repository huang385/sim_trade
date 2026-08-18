from datetime import date, datetime
from decimal import Decimal
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.order import Order
from app.models.account import Account
from app.enums.order_enums import LIMIT_LIKE_ORDER_TYPES, OffsetFlag, OrderStatus


SUPPORTED_ACTIVE_OFFSET_FLAGS = (
    OffsetFlag.OPEN.value,
    OffsetFlag.CLOSE.value,
    OffsetFlag.CLOSE_TODAY.value,
    OffsetFlag.CLOSE_YESTERDAY.value,
)


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
    def list_by_order_ids(
        db: Session,
        order_ids: Sequence[str],
    ) -> Sequence[Order]:
        """Batch-load active-index candidates without per-order queries."""

        normalized_ids = tuple(sorted({str(item) for item in order_ids if item}))
        if not normalized_ids:
            return ()
        return db.scalars(
            select(Order)
            .where(Order.order_id.in_(normalized_ids))
            .order_by(Order.id)
        ).all()

    @staticmethod
    def get_by_order_id_for_user(
        db: Session,
        *,
        order_id: str,
        user_id: str,
    ) -> Order | None:
        """一次查询返回普通用户有权访问的订单，隐藏其他用户资源存在性。"""

        return db.scalar(
            select(Order)
            .join(Account, Account.account_id == Order.account_id)
            .where(
                Order.order_id == order_id,
                Account.user_id == user_id,
            )
        )

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
    def get_by_order_id_for_user_for_update(
        db: Session,
        *,
        order_id: str,
        user_id: str,
    ) -> Order | None:
        """
        按普通用户可见范围查询并只锁定订单行。

        Account连接只用于所有权过滤；``FOR UPDATE OF orders``不会提前
        锁定账户，后续事务仍严格遵循Order→Account的固定锁顺序。
        """

        statement = (
            select(Order)
            .join(Account, Account.account_id == Order.account_id)
            .where(
                Order.order_id == order_id,
                Account.user_id == user_id,
            )
            .with_for_update(of=Order)
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
    def get_by_client_order_id_for_user(
        db: Session,
        *,
        account_id: str,
        client_order_id: str,
        user_id: str,
    ) -> Order | None:
        """在账户归属范围内查询幂等订单，不暴露其他用户订单。"""

        statement = (
            select(Order)
            .join(Account, Account.account_id == Order.account_id)
            .where(
                Order.account_id == account_id,
                Order.client_order_id == client_order_id,
                Account.user_id == user_id,
            )
        )
        return db.scalar(statement)

    @staticmethod
    def list_by_account(
        db: Session,
        account_id: str,
        *,
        after_id: int | None = None,
        limit: int = 100,
    ) -> Sequence[Order]:
        """使用自增主键游标有界查询账户订单。"""

        statement = select(Order).where(
            Order.account_id == account_id
        )
        if after_id is not None:
            statement = (
                statement.where(Order.id > after_id)
                .order_by(Order.id)
                .limit(limit)
            )
            return db.scalars(statement).all()
        rows = db.scalars(
            statement.order_by(Order.id.desc()).limit(limit)
        ).all()
        return list(reversed(rows))

    @staticmethod
    def list_page_by_account(
        db: Session,
        account_id: str,
        *,
        trading_day: date | None = None,
        before_id: int | None,
        fetch_size: int,
    ) -> Sequence[Order]:
        """
        按自增主键倒序读取一页订单。

        before_id使用严格小于，既不重复上一页最后一条，也会自然排除翻页期间
        新写入的更大主键订单，保证一次向历史方向遍历的顺序稳定。
        """

        statement = select(Order).where(
            Order.account_id == account_id
        )
        if trading_day is not None:
            statement = statement.where(Order.trading_day == trading_day)
        if before_id is not None:
            statement = statement.where(Order.id < before_id)
        return db.scalars(
            statement.order_by(Order.id.desc()).limit(fetch_size)
        ).all()

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
                Order.order_type.in_(LIMIT_LIKE_ORDER_TYPES),
                Order.offset_flag.in_(SUPPORTED_ACTIVE_OFFSET_FLAGS),
            )
            .order_by(Order.id)
            .limit(batch_size)
        )
        return db.scalars(statement).all()

    @staticmethod
    def list_active_option_sell_open_by_account(
        db: Session,
        account_id: str,
    ) -> Sequence[Order]:
        """
        查询账户全部活动商品期权卖出开仓订单。

        调用方已经持有 Account 行锁。这里故意不再取得 Order 行锁：订单
        调整、成交和撤单统一采用 Order→Account 顺序，如果反向锁定会引入
        死锁。并发订单事务提交后会再次产生 Account Dirty，完整估值将重试。
        """

        statement = select(Order).where(
            Order.account_id == account_id,
            Order.instrument_type == "FUTURES_OPTION",
            Order.direction == "SELL",
            Order.offset_flag == "OPEN",
            Order.order_type.in_(LIMIT_LIKE_ORDER_TYPES),
            Order.status.in_(
                (
                    OrderStatus.ACCEPTED.value,
                    OrderStatus.PARTIALLY_FILLED.value,
                )
            ),
            Order.remaining_volume > 0,
        )
        return db.scalars(statement.order_by(Order.id)).all()

    @staticmethod
    def list_active_open_by_account(
        db: Session, account_id: str
    ) -> Sequence[Order]:
        """返回风险处置需要撤销的活动开仓单，不包含任何平仓委托。"""

        return db.scalars(
            select(Order)
            .where(
                Order.account_id == account_id,
                Order.offset_flag == OffsetFlag.OPEN.value,
                Order.order_type.in_(LIMIT_LIKE_ORDER_TYPES),
                Order.status.in_(
                    (OrderStatus.ACCEPTED.value, OrderStatus.PARTIALLY_FILLED.value)
                ),
                Order.remaining_volume > 0,
            )
            .order_by(Order.id)
        ).all()

    @staticmethod
    def list_by_liquidation_task(
        db: Session, task_id: str
    ) -> Sequence[Order]:
        """查询强平任务已创建的订单，用于重启恢复和幂等判断。"""

        return db.scalars(
            select(Order)
            .where(Order.liquidation_task_id == task_id)
            .order_by(Order.id)
        ).all()

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
        offset_flag: str | None,
        order_type: str,
        commission_type: str | None,
        commission_parameter: Decimal | None,
        commission_contract_multiplier: Decimal | None,
        limit_price: Decimal,
        submitted_limit_price: Decimal | None = None,
        resolved_price: Decimal | None = None,
        market_protection_price: Decimal | None = None,
        price_snapshot_time: datetime | None = None,
        price_snapshot_source: str | None = None,
        price_snapshot_event_id: str | None = None,
        price_snapshot_stream_message_id: str | None = None,
        price_snapshot_bid1: Decimal | None = None,
        price_snapshot_bid_volume1: int | None = None,
        price_snapshot_ask1: Decimal | None = None,
        price_snapshot_ask_volume1: int | None = None,
        price_snapshot_last: Decimal | None = None,
        total_volume: int,
        status: str,
        submit_status: str,
        frozen_margin: Decimal,
        frozen_commission: Decimal,
        frozen_position_volume: int,
        created_at: datetime,
        accepted_at: datetime,
        instrument_type: str = "FUTURES",
        underlying_order_book_id: str | None = None,
        underlying_exchange_id: str | None = None,
        underlying_symbol: str | None = None,
        frozen_cash: Decimal = Decimal("0.000000"),
        margin_rule_id: int | None = None,
        margin_rule_version: str | None = None,
        margin_price_mode: str | None = None,
        margin_underlying_price: Decimal | None = None,
        margin_option_price: Decimal | None = None,
        margin_rule_snapshot: dict | None = None,
        margin_snapshot_schema_version: str | None = None,
        margin_calculation_version: str | None = None,
        fee_rule_id: int | None = None,
        fee_rule_version: str | None = None,
        fee_rule_snapshot: dict | None = None,
        order_source: str = "USER",
        liquidation_task_id: str | None = None,
        reduce_only: bool = False,
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
            instrument_type=instrument_type,
            underlying_order_book_id=underlying_order_book_id,
            underlying_exchange_id=underlying_exchange_id,
            underlying_symbol=underlying_symbol,
            direction=direction,
            offset_flag=offset_flag,
            order_type=order_type,
            commission_type=commission_type,
            commission_parameter=commission_parameter,
            commission_contract_multiplier=commission_contract_multiplier,
            fee_rule_id=fee_rule_id,
            fee_rule_version=fee_rule_version,
            fee_rule_snapshot=fee_rule_snapshot,
            limit_price=limit_price,
            submitted_limit_price=submitted_limit_price,
            resolved_price=resolved_price if resolved_price is not None else limit_price,
            market_protection_price=market_protection_price,
            price_snapshot_time=price_snapshot_time,
            price_snapshot_source=price_snapshot_source,
            price_snapshot_event_id=price_snapshot_event_id,
            price_snapshot_stream_message_id=price_snapshot_stream_message_id,
            price_snapshot_bid1=price_snapshot_bid1,
            price_snapshot_bid_volume1=price_snapshot_bid_volume1,
            price_snapshot_ask1=price_snapshot_ask1,
            price_snapshot_ask_volume1=price_snapshot_ask_volume1,
            price_snapshot_last=price_snapshot_last,
            total_volume=total_volume,
            # 新订单尚未撮合，成交数量从0开始，平均成交价暂时为空。
            traded_volume=0,
            remaining_volume=total_volume,
            # 新订单尚未撤销；后续主动撤单只累计当时的剩余数量。
            cancelled_volume=0,
            average_price=None,
            status=status,
            submit_status=submit_status,
            frozen_margin=frozen_margin,
            frozen_cash=frozen_cash,
            frozen_commission=frozen_commission,
            frozen_position_volume=frozen_position_volume,
            margin_rule_id=margin_rule_id,
            margin_rule_version=margin_rule_version,
            margin_price_mode=margin_price_mode,
            margin_underlying_price=margin_underlying_price,
            margin_option_price=margin_option_price,
            margin_rule_snapshot=margin_rule_snapshot,
            margin_snapshot_schema_version=margin_snapshot_schema_version,
            margin_calculation_version=margin_calculation_version,
            order_source=order_source,
            liquidation_task_id=liquidation_task_id,
            reduce_only=reduce_only,
            reject_code=None,
            reject_message=None,
            cancel_reason_code=None,
            cancel_reason_message=None,
            created_at=created_at,
            accepted_at=accepted_at,
            updated_at=accepted_at,
        )

        # 只加入当前 Session，是否提交由上层事务决定。
        db.add(order)
        return order
