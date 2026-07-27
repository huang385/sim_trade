from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.common.decimal_utils import quantize_money
from app.common.exceptions import (
    AppError,
    DataAccessError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from app.common.time_utils import utc_now
from app.enums.order_enums import (
    OffsetFlag,
    OrderStatus,
    OrderType,
    PositionDirection,
    PositionFreezeAllocationStatus,
)
from app.models.order import Order
from app.repositories.account_repository import AccountRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.outbox_repository import OutboxRepository
from app.repositories.position_freeze_allocation_repository import (
    PositionFreezeAllocationRepository,
)
from app.repositories.position_repository import PositionRepository
from app.schemas.order_schema import OrderCancelRequest
from app.services.order_freeze_service import OrderFreezeService


def generate_cancel_event_id() -> str:
    """生成不依赖单进程计数器的撤单事件编号。"""

    return f"EVT-{uuid4().hex.upper()}"


def _decimal_string(value: Decimal) -> str:
    """按数据库六位小数精度生成事件金额字符串，禁止经过 float。"""

    return format(quantize_money(value), "f")


class OrderCancellationService:
    """
    期货限价开平仓订单主动撤销的事务服务。

    撤单与成交统一先锁 Order、再锁 Account。订单更新、剩余冻结资源释放
    和撤单 Outbox 事件必须在同一个 PostgreSQL 事务中成功或回滚。
    """

    CANCELLABLE_STATUSES = {
        OrderStatus.ACCEPTED.value,
        OrderStatus.PARTIALLY_FILLED.value,
    }
    IDEMPOTENT_STATUSES = {
        OrderStatus.CANCELLED.value,
        OrderStatus.PARTIALLY_CANCELLED.value,
    }

    def __init__(
        self,
        *,
        order_repository: OrderRepository | None = None,
        account_repository: AccountRepository | None = None,
        freeze_service: OrderFreezeService | None = None,
        outbox_repository: OutboxRepository | None = None,
        position_repository: PositionRepository | None = None,
        allocation_repository: PositionFreezeAllocationRepository | None = None,
        event_id_factory: Callable[[], str] = generate_cancel_event_id,
        time_provider: Callable[[], datetime] = utc_now,
    ):
        self.order_repository = order_repository or OrderRepository()
        self.account_repository = account_repository or AccountRepository()
        self.freeze_service = freeze_service or OrderFreezeService()
        self.outbox_repository = outbox_repository or OutboxRepository()
        self.position_repository = position_repository or PositionRepository()
        self.allocation_repository = (
            allocation_repository or PositionFreezeAllocationRepository()
        )
        self.event_id_factory = event_id_factory
        self.time_provider = time_provider

    def cancel_order(
        self,
        *,
        db: Session,
        order_id: str,
        request: OrderCancelRequest,
    ) -> Order:
        """锁定订单和账户，释放剩余冻结资源并原子提交撤单事件。"""

        normalized_order_id = order_id.strip()
        normalized_account_id = request.account_id.strip()
        try:
            # 固定锁顺序第一步：先锁订单，与成交结算保持一致。
            order = self.order_repository.get_by_order_id_for_update(
                db,
                normalized_order_id,
            )
            if order is None:
                raise ResourceNotFoundError(
                    "订单不存在",
                    error_code="ORDER_NOT_FOUND",
                )
            if order.account_id != normalized_account_id:
                raise ResourceConflictError(
                    "订单不属于指定账户",
                    error_code="ORDER_ACCOUNT_MISMATCH",
                )
            # 已撤销终态直接幂等返回，不锁账户、不释放资金、不创建新事件，
            # cancelled_at 也保持第一次撤单写入的值。订单查询使用了
            # SELECT FOR UPDATE，因此返回前必须主动提交并刷新，及时释放行锁。
            if order.status in self.IDEMPOTENT_STATUSES:
                db.commit()
                db.refresh(order)
                return order

            if order.order_type != OrderType.LIMIT.value:
                raise ResourceConflictError(
                    "当前撤单服务仅支持限价订单",
                    error_code="ORDER_NOT_CANCELLABLE",
                )
            if order.status not in self.CANCELLABLE_STATUSES:
                raise ResourceConflictError(
                    "订单当前状态不允许撤销",
                    error_code="ORDER_NOT_CANCELLABLE",
                )
            if order.remaining_volume <= 0:
                raise DataAccessError(
                    "活动订单剩余数量不合法",
                    error_code="CANCEL_ORDER_STATE_INCONSISTENT",
                )
            if order.offset_flag == OffsetFlag.OPEN.value:
                if order.frozen_position_volume != 0:
                    raise DataAccessError(
                        "开仓订单冻结持仓数量不为0",
                        error_code="CANCEL_ORDER_STATE_INCONSISTENT",
                    )
            elif order.offset_flag in {
                OffsetFlag.CLOSE.value,
                OffsetFlag.CLOSE_TODAY.value,
                OffsetFlag.CLOSE_YESTERDAY.value,
            }:
                if (
                    quantize_money(order.frozen_margin)
                    != Decimal("0.000000")
                    or order.frozen_position_volume
                    != order.remaining_volume
                ):
                    raise DataAccessError(
                        "平仓订单冻结资源与剩余数量不一致",
                        error_code="CANCEL_ORDER_STATE_INCONSISTENT",
                    )
            else:
                raise ResourceConflictError(
                    "当前订单开平标志不支持撤单",
                    error_code="ORDER_NOT_CANCELLABLE",
                )

            cancelled_at = self.time_provider()
            # 固定锁顺序第二步：先锁账户；平仓撤单随后继续锁持仓、
            # 逐笔明细和本订单Allocation，开仓撤单不访问持仓。
            account = self.account_repository.get_by_account_id_for_update(
                db,
                order.account_id,
            )
            cancel_volume = order.remaining_volume
            released_margin = quantize_money(order.frozen_margin)
            released_commission = quantize_money(order.frozen_commission)

            if order.offset_flag == OffsetFlag.OPEN.value:
                self.freeze_service.release_open_order_frozen_resources(
                    account=account,
                    frozen_margin=released_margin,
                    frozen_commission=released_commission,
                )
            elif order.offset_flag in {
                OffsetFlag.CLOSE.value,
                OffsetFlag.CLOSE_TODAY.value,
                OffsetFlag.CLOSE_YESTERDAY.value,
            }:
                position_direction = (
                    PositionDirection.LONG.value
                    if order.direction == "SELL"
                    else PositionDirection.SHORT.value
                )
                position = self.position_repository.get_for_update(
                    db,
                    account_id=order.account_id,
                    exchange_id=order.exchange_id,
                    symbol=order.symbol,
                    direction=position_direction,
                )
                if position is None:
                    raise DataAccessError(
                        "平仓撤单对应持仓不存在",
                        error_code="CANCEL_POSITION_INCONSISTENT",
                    )
                if position.direction != position_direction:
                    raise DataAccessError(
                        "平仓撤单对应持仓方向不一致",
                        error_code="CANCEL_POSITION_INCONSISTENT",
                    )
                allocations = (
                    self.allocation_repository.list_by_order_for_update(
                        db,
                        order.order_id,
                    )
                )
                allocation_detail_ids = list(
                    dict.fromkeys(
                        item.position_detail_id
                        for item in allocations
                    )
                )
                details = (
                    self.position_repository
                    .list_details_by_ids_for_update(
                        db,
                        position_id=position.position_id,
                        position_detail_ids=allocation_detail_ids,
                    )
                )
                detail_map = {
                    item.position_detail_id: item for item in details
                }
                allocation_volume = sum(
                    item.remaining_frozen_volume for item in allocations
                )
                allocation_commission = quantize_money(
                    sum(
                        (
                            item.remaining_frozen_commission
                            for item in allocations
                        ),
                        Decimal("0"),
                    )
                )
                if (
                    not allocations
                    or allocation_volume != cancel_volume
                    or allocation_volume != order.frozen_position_volume
                    or allocation_commission != released_commission
                ):
                    raise DataAccessError(
                        "平仓撤单冻结分配资源不一致",
                        error_code="CANCEL_POSITION_INCONSISTENT",
                    )

                # 在修改任何持仓明细或 Allocation 前完成全量一致性校验，
                # 防止前几条已修改、后续才发现脏数据。即使异常最终会回滚，
                # 提前校验也让事务内对象始终保持清晰的全有或全无状态。
                for allocation in allocations:
                    detail = detail_map.get(
                        allocation.position_detail_id
                    )
                    if (
                        allocation.order_id != order.order_id
                        or allocation.position_id != position.position_id
                        or allocation.account_id != order.account_id
                        or allocation.exchange_id != order.exchange_id
                        or allocation.symbol != order.symbol
                        or allocation.resolved_offset_flag
                        not in {
                            OffsetFlag.CLOSE_TODAY.value,
                            OffsetFlag.CLOSE_YESTERDAY.value,
                        }
                        or detail is None
                        or detail.direction != position_direction
                        or allocation.resolved_offset_flag
                        != (
                            OffsetFlag.CLOSE_TODAY.value
                            if detail.open_trading_day
                            == order.trading_day
                            else (
                                OffsetFlag.CLOSE_YESTERDAY.value
                                if detail.open_trading_day
                                < order.trading_day
                                else None
                            )
                        )
                        or detail.frozen_volume
                        < allocation.remaining_frozen_volume
                        or detail.remaining_volume
                        < allocation.remaining_frozen_volume
                        or allocation.original_frozen_volume
                        != allocation.remaining_frozen_volume
                        + allocation.consumed_volume
                        + allocation.released_volume
                        or quantize_money(
                            allocation.original_frozen_commission
                        )
                        != quantize_money(
                            allocation.remaining_frozen_commission
                            + allocation.consumed_commission
                            + allocation.released_commission
                        )
                    ):
                        raise DataAccessError(
                            "平仓撤单逐笔冻结资源不一致",
                            error_code="CANCEL_POSITION_INCONSISTENT",
                        )

                for allocation in allocations:
                    released_volume = allocation.remaining_frozen_volume
                    released_allocation_commission = (
                        allocation.remaining_frozen_commission
                    )
                    if (
                        released_volume <= 0
                        and released_allocation_commission
                        == Decimal("0")
                    ):
                        continue
                    detail = detail_map[allocation.position_detail_id]
                    detail.frozen_volume -= released_volume
                    detail.updated_at = cancelled_at
                    allocation.remaining_frozen_volume = 0
                    allocation.released_volume += released_volume
                    allocation.remaining_frozen_commission = Decimal(
                        "0.000000"
                    )
                    allocation.released_commission = quantize_money(
                        allocation.released_commission
                        + released_allocation_commission
                    )
                    allocation.status = (
                        PositionFreezeAllocationStatus.RELEASED.value
                    )
                    allocation.updated_at = cancelled_at
                if position.frozen_volume < cancel_volume:
                    raise DataAccessError(
                        "持仓汇总冻结数量不足",
                        error_code="CANCEL_POSITION_INCONSISTENT",
                    )
                position.frozen_volume -= cancel_volume
                position.available_volume += cancel_volume
                position.updated_at = cancelled_at
                self.freeze_service.release_close_order_commission(
                    account=account,
                    frozen_commission=released_commission,
                )
                order.frozen_position_volume = 0

            order.cancelled_volume += cancel_volume
            order.remaining_volume = 0
            order.frozen_margin = Decimal("0.000000")
            order.frozen_commission = Decimal("0.000000")
            order.cancelled_at = cancelled_at
            order.updated_at = cancelled_at
            if order.traded_volume == 0:
                order.status = OrderStatus.CANCELLED.value
                event_type = "ORDER_CANCELLED"
            else:
                order.status = OrderStatus.PARTIALLY_CANCELLED.value
                event_type = "ORDER_PARTIALLY_CANCELLED"

            if (
                order.total_volume
                != order.traded_volume
                + order.remaining_volume
                + order.cancelled_volume
            ):
                raise DataAccessError(
                    "撤单后订单数量不守恒",
                    error_code="CANCEL_ORDER_VOLUME_INCONSISTENT",
                )

            event_id = self.event_id_factory()
            self.outbox_repository.create_event(
                db=db,
                event_id=event_id,
                aggregate_type="ORDER",
                aggregate_id=order.order_id,
                event_type=event_type,
                payload={
                    "event_id": event_id,
                    "event_type": event_type,
                    "order_id": order.order_id,
                    "client_order_id": order.client_order_id,
                    "account_id": order.account_id,
                    "exchange_id": order.exchange_id,
                    "symbol": order.symbol,
                    "order_book_id": order.order_book_id,
                    "trading_day": order.trading_day.isoformat(),
                    "direction": order.direction,
                    "offset_flag": order.offset_flag,
                    "order_type": order.order_type,
                    "status": order.status,
                    "total_volume": order.total_volume,
                    "traded_volume": order.traded_volume,
                    "remaining_volume": order.remaining_volume,
                    "cancelled_volume": order.cancelled_volume,
                    "average_price": (
                        _decimal_string(order.average_price)
                        if order.average_price is not None
                        else ""
                    ),
                    "released_margin": _decimal_string(released_margin),
                    "released_commission": _decimal_string(
                        released_commission
                    ),
                    "frozen_margin": _decimal_string(order.frozen_margin),
                    "frozen_commission": _decimal_string(
                        order.frozen_commission
                    ),
                    "frozen_position_volume": order.frozen_position_volume,
                    "cancelled_at": cancelled_at.isoformat(),
                    "updated_at": order.updated_at.isoformat(),
                },
                created_at=cancelled_at,
            )

            # Redis 不参与本事务；即使暂时不可用，数据库和 PENDING Outbox
            # 仍可正常提交，之后由发布 Worker 重试。
            db.commit()
            db.refresh(order)
            return order

        except AppError:
            db.rollback()
            raise
        except SQLAlchemyError as exc:
            db.rollback()
            raise DataAccessError(
                "撤销订单失败",
                error_code="ORDER_CANCEL_FAILED",
            ) from exc
        except Exception:
            db.rollback()
            raise


def get_order_cancellation_service() -> OrderCancellationService:
    """创建供 FastAPI Depends 使用的撤单事务服务。"""

    return OrderCancellationService()
