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
from app.enums.order_enums import OffsetFlag, OrderStatus, OrderType
from app.models.order import Order
from app.repositories.account_repository import AccountRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.outbox_repository import OutboxRepository
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
    限价开仓订单主动撤销的事务服务。

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
        event_id_factory: Callable[[], str] = generate_cancel_event_id,
        time_provider: Callable[[], datetime] = utc_now,
    ):
        self.order_repository = order_repository or OrderRepository()
        self.account_repository = account_repository or AccountRepository()
        self.freeze_service = freeze_service or OrderFreezeService()
        self.outbox_repository = outbox_repository or OutboxRepository()
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

            # 当前服务只实现“限价开仓订单”的资金释放。这里必须再次校验
            # 数据库中的订单类型和开平标志，不能仅依赖下单入口的校验，
            # 否则未来平仓订单进入活动状态后可能误用开仓撤单流程。
            if (
                order.order_type != OrderType.LIMIT.value
                or order.offset_flag != OffsetFlag.OPEN.value
            ):
                raise ResourceConflictError(
                    "当前撤单服务仅支持限价开仓订单",
                    error_code="ORDER_NOT_CANCELLABLE",
                )

            # 开仓订单只冻结资金，不应冻结任何已有持仓。非零值表示订单
            # 数据已经违反业务约束，必须中止并回滚，不能通过清零掩盖问题。
            if order.frozen_position_volume != 0:
                raise DataAccessError(
                    "开仓订单冻结持仓数量不为0",
                    error_code="CANCEL_ORDER_STATE_INCONSISTENT",
                )

            # 已撤销终态直接幂等返回，不锁账户、不释放资金、不创建新事件，
            # cancelled_at 也保持第一次撤单写入的值。订单查询使用了
            # SELECT FOR UPDATE，因此返回前必须主动提交并刷新，及时释放行锁。
            if order.status in self.IDEMPOTENT_STATUSES:
                db.commit()
                db.refresh(order)
                return order
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

            # 固定锁顺序第二步：订单锁成功后再锁账户。撤单不访问持仓。
            account = self.account_repository.get_by_account_id_for_update(
                db,
                order.account_id,
            )
            cancel_volume = order.remaining_volume
            released_margin = quantize_money(order.frozen_margin)
            released_commission = quantize_money(order.frozen_commission)
            self.freeze_service.release_open_order_frozen_resources(
                account=account,
                frozen_margin=released_margin,
                frozen_commission=released_commission,
            )

            cancelled_at = self.time_provider()
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
