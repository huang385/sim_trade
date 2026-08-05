import json
from dataclasses import dataclass
from typing import Mapping

from sqlalchemy.orm import Session

from app.enums.order_enums import (
    OffsetFlag,
    OrderDirection,
    OrderStatus,
    OrderType,
)
from app.infrastructure.active_order_index import ActiveOrderIndex
from app.matching.models import MatchingOrder, MatchingOrderCandidate
from app.repositories.order_repository import OrderRepository


class OrderEventValidationError(ValueError):
    """订单事件格式不合法，消息应保留并按失败策略重试。"""


class UnsupportedOrderEventError(OrderEventValidationError):
    """当前 Consumer 不支持的事件类型，可以直接转入死信。"""


@dataclass(frozen=True)
class ParsedOrderEvent:
    """完成基础校验后的订单或成交事件。"""

    event_id: str
    event_type: str
    order_id: str
    account_id: str
    exchange_id: str
    symbol: str
    payload: dict


@dataclass(frozen=True)
class AcceptedOrderProcessResult:
    """单条订单事件的业务处理结果。"""

    event_id: str
    event_type: str
    order_id: str
    exchange_id: str
    symbol: str
    action: str
    order_snapshot: MatchingOrderCandidate | None = None


class AcceptedOrderEventService:
    """
    校验订单状态事件并维护 Redis 活动订单索引。

    事件仅用于定位订单，是否注册以及写入哪些字段全部以 PostgreSQL 中的
    orders 最新记录为准。本服务不会修改账户、订单状态、成交或持仓。
    """

    ACTIVE_STATUSES = {
        OrderStatus.ACCEPTED.value,
        OrderStatus.PARTIALLY_FILLED.value,
    }
    TERMINAL_STATUSES = {
        OrderStatus.FILLED.value,
        OrderStatus.CANCELLED.value,
        OrderStatus.PARTIALLY_CANCELLED.value,
        OrderStatus.REJECTED.value,
    }
    INDEX_EVENT_TYPES = {
        "ORDER_ACCEPTED",
        "ORDER_PARTIALLY_FILLED",
        "ORDER_FILLED",
        "ORDER_MARGIN_UPDATED",
        "ORDER_CANCELLED",
        "ORDER_PARTIALLY_CANCELLED",
    }
    # TRADE_CREATED 与订单状态事件发布到同一 Stream。活动订单消费者只需
    # 安全确认它，不维护成交派生数据，避免把合法成交事件误送入死信。
    PASSTHROUGH_EVENT_TYPES = {
        "TRADE_CREATED",
        "POSITION_UPDATED",
        "POSITION_CLOSED",
        "ACCOUNT_FACT_UPDATED",
        "RISK_WARNING",
        "RISK_STATE_CHANGED",
        "LIQUIDATION_STARTED",
        "LIQUIDATION_ORDER_UPDATED",
        "LIQUIDATION_COMPLETED",
        "LIQUIDATION_FAILED",
        # 兼容升级前已存在的账户事实消息。
        "ACCOUNT_UPDATED",
    }
    SUPPORTED_OFFSET_FLAGS = {
        OffsetFlag.OPEN.value,
        OffsetFlag.CLOSE.value,
        OffsetFlag.CLOSE_TODAY.value,
        OffsetFlag.CLOSE_YESTERDAY.value,
    }

    def __init__(
        self,
        *,
        order_repository: OrderRepository,
        active_order_index: ActiveOrderIndex,
        processed_ttl_seconds: int,
    ):
        self.order_repository = order_repository
        self.active_order_index = active_order_index
        self.processed_ttl_seconds = processed_ttl_seconds

    @staticmethod
    def parse_event(fields: Mapping[str, str]) -> ParsedOrderEvent:
        """解析并校验 Redis Stream 中的基础事件结构。"""

        event_id = fields.get("event_id", "").strip()
        event_type = fields.get("event_type", "").strip()
        if not event_id:
            raise OrderEventValidationError("事件缺少event_id")
        if not event_type:
            raise OrderEventValidationError("事件缺少event_type")
        if event_type not in (
            AcceptedOrderEventService.INDEX_EVENT_TYPES
            | AcceptedOrderEventService.PASSTHROUGH_EVENT_TYPES
        ):
            raise UnsupportedOrderEventError(
                f"不支持的订单事件类型: {event_type}"
            )

        raw_payload = fields.get("payload")
        try:
            payload = json.loads(raw_payload)
        except (TypeError, json.JSONDecodeError) as exc:
            raise OrderEventValidationError("payload不是合法JSON") from exc
        if not isinstance(payload, dict):
            raise OrderEventValidationError("payload必须是JSON对象")
        if "event_type" in payload and payload.get("event_type") != event_type:
            raise OrderEventValidationError("event_type与payload不一致")

        required_fields = (
            ("account_id",)
            if event_type in AcceptedOrderEventService.PASSTHROUGH_EVENT_TYPES
            else ("order_id", "account_id", "exchange_id", "symbol")
        )
        missing = [
            name
            for name in required_fields
            if not isinstance(payload.get(name), str)
            or not payload[name].strip()
        ]
        if missing:
            raise OrderEventValidationError(
                f"payload缺少必要字段: {','.join(missing)}"
            )

        return ParsedOrderEvent(
            event_id=event_id,
            event_type=event_type,
            order_id=str(payload.get("order_id") or "").strip(),
            account_id=payload["account_id"].strip(),
            exchange_id=str(payload.get("exchange_id") or "").strip(),
            symbol=str(payload.get("symbol") or "").strip(),
            payload=payload,
        )

    def process(
        self,
        db: Session,
        fields: Mapping[str, str],
    ) -> AcceptedOrderProcessResult:
        """处理一条事件，返回动作说明；异常由 Worker 决定重试或死信。"""

        event = self.parse_event(fields)
        if event.event_type in self.PASSTHROUGH_EVENT_TYPES:
            return AcceptedOrderProcessResult(
                event_id=event.event_id,
                event_type=event.event_type,
                order_id=event.order_id,
                exchange_id=event.exchange_id,
                symbol=event.symbol,
                action="IGNORED_TRADE_EVENT",
            )
        order = self.order_repository.get_by_order_id(db, event.order_id)

        # Outbox 和订单原子提交，正常情况下不会缺失；若确实找不到，Redis
        # 不创建任何索引，消息可安全 ACK，数据库仍是最终事实来源。
        if order is None:
            return AcceptedOrderProcessResult(
                event_id=event.event_id,
                event_type=event.event_type,
                order_id=event.order_id,
                exchange_id=event.exchange_id,
                symbol=event.symbol,
                action="ORDER_NOT_FOUND",
            )

        if order.order_id != event.order_id:
            raise OrderEventValidationError("数据库订单编号与事件不一致")
        if order.account_id != event.account_id:
            raise OrderEventValidationError("事件账户与数据库订单账户不一致")

        should_remove = (
            order.status in self.TERMINAL_STATUSES
            or order.remaining_volume <= 0
            or order.status not in self.ACTIVE_STATUSES
            or order.order_type != OrderType.LIMIT.value
            or order.offset_flag not in self.SUPPORTED_OFFSET_FLAGS
        )
        if should_remove:
            self.active_order_index.remove_active_order(
                order_id=order.order_id,
                account_id=order.account_id,
                exchange_id=order.exchange_id,
                symbol=order.symbol,
                event_id=event.event_id,
                processed_ttl_seconds=self.processed_ttl_seconds,
            )
            return AcceptedOrderProcessResult(
                event_id=event.event_id,
                event_type=event.event_type,
                order_id=event.order_id,
                exchange_id=order.exchange_id,
                symbol=order.symbol,
                action="REMOVED",
            )

        written = self.active_order_index.add_active_order(
            order,
            event_id=event.event_id,
            processed_ttl_seconds=self.processed_ttl_seconds,
        )
        return AcceptedOrderProcessResult(
            event_id=event.event_id,
            event_type=event.event_type,
            order_id=event.order_id,
            exchange_id=order.exchange_id,
            symbol=order.symbol,
            action=(
                "UPDATED"
                if written
                and event.event_type
                in {"ORDER_PARTIALLY_FILLED", "ORDER_MARGIN_UPDATED"}
                else "REGISTERED"
                if written
                else "DUPLICATE"
            ),
            order_snapshot=MatchingOrderCandidate(
                order_id=order.order_id,
                exchange_id=order.exchange_id,
                symbol=order.symbol,
                status=OrderStatus(order.status),
                order=MatchingOrder(
                    direction=OrderDirection(order.direction),
                    offset_flag=OffsetFlag(order.offset_flag),
                    order_type=OrderType(order.order_type),
                    limit_price=order.limit_price,
                    remaining_volume=order.remaining_volume,
                ),
            ),
        )
