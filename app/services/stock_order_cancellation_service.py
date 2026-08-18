from datetime import datetime
from decimal import Decimal
from typing import Callable
from uuid import uuid4

from sqlalchemy.orm import Session

from app.common.decimal_utils import quantize_money
from app.common.exceptions import (
    BusinessRuleError,
    DataAccessError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from app.common.time_utils import utc_now
from app.core.config import settings
from app.enums.account_enums import AccountType
from app.enums.order_enums import OrderStatus
from app.repositories.account_repository import AccountRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.outbox_repository import OutboxRepository
from app.repositories.position_repository import PositionRepository
from app.schemas.order_schema import OrderCancelRequest
from app.services.account_access_scope import AccountAccessScope
from app.services.cash_security_funds_service import CashSecurityFundsService
from app.services.stock_order_validation_service import (
    CASH_SECURITY_POSITION_DIRECTION,
)


class CashSecurityOrderCancellationService:
    """股票主动撤单：按 Order → Account → Position 的锁顺序释放资源。"""

    ACTIVE_STATUSES = {OrderStatus.ACCEPTED.value, OrderStatus.PARTIALLY_FILLED.value}
    TERMINAL_STATUSES = {
        OrderStatus.CANCELLED.value,
        OrderStatus.FILLED.value,
        OrderStatus.PARTIALLY_CANCELLED.value,
        OrderStatus.REJECTED.value,
    }

    def __init__(
        self,
        *,
        order_repository: OrderRepository | None = None,
        account_repository: AccountRepository | None = None,
        position_repository: PositionRepository | None = None,
        outbox_repository: OutboxRepository | None = None,
        instrument_type: str = "STOCK",
        cancelled_event_type: str = "STOCK_ORDER_CANCELLED",
        time_provider: Callable[[], datetime] = utc_now,
    ) -> None:
        self.order_repository = order_repository or OrderRepository()
        self.account_repository = account_repository or AccountRepository()
        self.position_repository = position_repository or PositionRepository()
        self.outbox_repository = outbox_repository or OutboxRepository()
        self.instrument_type = instrument_type
        self.cancelled_event_type = cancelled_event_type
        self.time_provider = time_provider

    @staticmethod
    def _not_found(scope: AccountAccessScope) -> ResourceNotFoundError:
        return ResourceNotFoundError(
            "目标资源不存在",
            error_code="RESOURCE_NOT_FOUND" if scope.conceal_resource_existence else "ORDER_NOT_FOUND",
        )

    def cancel_order(
        self,
        *,
        db: Session,
        order_id: str,
        request: OrderCancelRequest,
        access_scope: AccountAccessScope,
    ):
        if not settings.stock_order_entry_enabled:
            raise BusinessRuleError(
                "股票订单受理尚未启用",
                error_code="STOCK_ORDER_ENTRY_DISABLED",
            )
        try:
            normalized_order_id = order_id.strip()
            order = (
                self.order_repository.get_by_order_id_for_update(db, normalized_order_id)
                if access_scope.is_admin
                else self.order_repository.get_by_order_id_for_user_for_update(
                    db, order_id=normalized_order_id, user_id=access_scope.user_id
                )
            )
            if order is None:
                raise self._not_found(access_scope)
            if order.instrument_type != self.instrument_type:
                raise ResourceConflictError(
                    "该订单不是股票订单", error_code="STOCK_ORDER_REQUIRED"
                )
            account = (
                self.account_repository.get_by_account_id_for_update(db, order.account_id)
                if access_scope.is_admin
                else self.account_repository.get_owned_account_for_update(
                    db, account_id=order.account_id, user_id=access_scope.user_id
                )
            )
            if account is None:
                raise self._not_found(access_scope)
            if account.account_type not in {AccountType.STOCK.value, "SECURITIES_CASH"}:
                raise DataAccessError(
                    "股票订单关联的账户类型不一致",
                    error_code="STOCK_ORDER_ACCOUNT_INCONSISTENT",
                )
            if request.account_id.strip() != order.account_id:
                raise ResourceConflictError(
                    "订单不属于指定账户", error_code="ORDER_ACCOUNT_MISMATCH"
                )
            if order.status in self.TERMINAL_STATUSES:
                db.expunge(order)
                db.commit()
                return order
            if order.status not in self.ACTIVE_STATUSES or order.remaining_volume <= 0:
                raise ResourceConflictError(
                    "订单当前不可撤销", error_code="ORDER_NOT_CANCELLABLE"
                )

            cancelled_at = self.time_provider()
            released_cash = quantize_money(order.frozen_cash)
            released_commission = quantize_money(order.frozen_commission)
            released_position = order.frozen_position_volume
            if order.direction == "BUY":
                if released_position != 0 or order.frozen_margin != Decimal("0"):
                    raise DataAccessError(
                        "股票买入订单冻结状态不一致",
                        error_code="STOCK_CANCEL_STATE_INCONSISTENT",
                    )
                CashSecurityFundsService.release_buy(
                    account=account,
                    frozen_cash=released_cash,
                    frozen_commission=released_commission,
                )
            elif order.direction == "SELL":
                if released_cash != Decimal("0") or released_commission != Decimal("0"):
                    raise DataAccessError(
                        "股票卖出订单不应冻结现金或手续费",
                        error_code="STOCK_CANCEL_STATE_INCONSISTENT",
                    )
                if released_position != order.remaining_volume:
                    raise DataAccessError(
                        "股票卖出订单冻结数量不一致",
                        error_code="STOCK_CANCEL_STATE_INCONSISTENT",
                    )
                position = self.position_repository.get_for_update(
                    db,
                    account_id=order.account_id,
                    exchange_id=order.exchange_id,
                    symbol=order.symbol,
                    direction=CASH_SECURITY_POSITION_DIRECTION,
                )
                if position is None or position.frozen_volume < released_position:
                    raise DataAccessError(
                        "股票卖出订单持仓冻结状态不一致",
                        error_code="STOCK_CANCEL_POSITION_INCONSISTENT",
                    )
                position.frozen_volume -= released_position
                position.available_volume = (
                    position.total_volume
                    - position.frozen_volume
                    - position.settlement_locked_volume
                )
                position.updated_at = cancelled_at
            else:
                raise DataAccessError(
                    "股票订单方向不合法", error_code="STOCK_CANCEL_STATE_INCONSISTENT"
                )

            cancel_volume = order.remaining_volume
            order.cancelled_volume += cancel_volume
            order.remaining_volume = 0
            order.frozen_cash = Decimal("0")
            order.frozen_commission = Decimal("0")
            order.frozen_position_volume = 0
            order.cancelled_at = cancelled_at
            order.updated_at = cancelled_at
            order.cancel_reason_code = "USER_REQUEST"
            order.cancel_reason_message = "用户主动撤销股票订单"
            order.status = (
                OrderStatus.CANCELLED.value
                if order.traded_volume == 0
                else OrderStatus.PARTIALLY_CANCELLED.value
            )
            if (
                order.total_volume
                != order.traded_volume + order.remaining_volume + order.cancelled_volume
            ):
                raise DataAccessError(
                    "股票撤单后的订单数量不一致",
                    error_code="STOCK_CANCEL_VOLUME_INCONSISTENT",
                )
            account.updated_at = cancelled_at
            event_id = f"SE-{uuid4().hex.upper()}"
            outbox_event = self.outbox_repository.create_event(
                db,
                event_id=event_id,
                aggregate_type="ORDER",
                aggregate_id=order.order_id,
                event_type=self.cancelled_event_type,
                created_at=cancelled_at,
                payload={
                    "event_type": self.cancelled_event_type,
                    "event_id": event_id,
                    "account_id": order.account_id,
                    "account_type": "SECURITIES_CASH",
                    "order_id": order.order_id,
                    "instrument_type": self.instrument_type,
                    "order_book_id": order.order_book_id,
                    "exchange_id": order.exchange_id,
                    "symbol": order.symbol,
                    "status": order.status,
                    "direction": order.direction,
                    "offset_flag": None,
                    "trading_day": order.trading_day.isoformat(),
                    "created_at": cancelled_at.isoformat(),
                },
            )
            db.flush()
            outbox_event.payload = {
                **outbox_event.payload,
                "business_version": str(outbox_event.id),
            }
            db.commit()
            return order
        except Exception:
            db.rollback()
            raise


_stock_order_cancellation_service = CashSecurityOrderCancellationService()


def get_stock_order_cancellation_service() -> CashSecurityOrderCancellationService:
    return _stock_order_cancellation_service


# 保持阶段二已发布的 Python 入口兼容。
StockOrderCancellationService = CashSecurityOrderCancellationService
