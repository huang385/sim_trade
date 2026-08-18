"""现金证券订单 Outbox 到活动订单索引的投影。

这里只维护 Redis 索引；不构造衍生品 ``MatchingOrder``，也不读取开平仓标志。
"""

import json
from dataclasses import dataclass
from typing import Mapping

from sqlalchemy.orm import Session

from app.enums.order_enums import OrderStatus
from app.infrastructure.active_order_index import ActiveOrderIndex
from app.repositories.order_repository import OrderRepository


class CashSecurityOrderEventError(ValueError):
    pass


@dataclass(frozen=True)
class CashSecurityOrderEventResult:
    event_id: str
    event_type: str
    order_id: str
    exchange_id: str
    symbol: str
    action: str
    order_snapshot: None = None


class CashSecurityOrderEventService:
    ACCEPT_EVENT = "STOCK_ORDER_ACCEPTED"
    CANCEL_EVENT = "STOCK_ORDER_CANCELLED"
    EVENT_TYPES = frozenset({ACCEPT_EVENT, CANCEL_EVENT})
    ACTIVE_STATUSES = frozenset(
        {OrderStatus.ACCEPTED.value, OrderStatus.PARTIALLY_FILLED.value}
    )

    def __init__(
        self,
        *,
        order_repository: OrderRepository,
        active_order_index: ActiveOrderIndex,
        processed_ttl_seconds: int,
    ) -> None:
        self.order_repository = order_repository
        self.active_order_index = active_order_index
        self.processed_ttl_seconds = processed_ttl_seconds

    @classmethod
    def is_cash_security_event(cls, fields: Mapping[str, str]) -> bool:
        return fields.get("event_type", "").strip() in cls.EVENT_TYPES

    @classmethod
    def _parse(cls, fields: Mapping[str, str]) -> tuple[str, str, dict]:
        event_id = fields.get("event_id", "").strip()
        event_type = fields.get("event_type", "").strip()
        if not event_id or event_type not in cls.EVENT_TYPES:
            raise CashSecurityOrderEventError("无效的现金证券订单事件")
        try:
            payload = json.loads(fields.get("payload", ""))
        except (TypeError, json.JSONDecodeError) as exc:
            raise CashSecurityOrderEventError("现金证券订单事件 payload 非法") from exc
        if not isinstance(payload, dict):
            raise CashSecurityOrderEventError("现金证券订单事件 payload 必须为对象")
        required = ("order_id", "account_id", "exchange_id", "symbol")
        if any(not str(payload.get(key) or "").strip() for key in required):
            raise CashSecurityOrderEventError("现金证券订单事件缺少定位字段")
        if payload.get("event_type") not in (None, event_type):
            raise CashSecurityOrderEventError("现金证券订单事件类型不一致")
        if str(payload.get("instrument_type") or "").upper() != "STOCK":
            raise CashSecurityOrderEventError("现金证券订单事件合约类型不一致")
        # 阶段二已落库而尚未发出的事件没有 account_type；它们仍需能
        # 依据数据库 STOCK 订单安全重建索引。新事件则必须使用规范值。
        account_type = str(payload.get("account_type") or "").upper()
        if account_type and account_type != "SECURITIES_CASH":
            raise CashSecurityOrderEventError("现金证券订单事件账户类型不一致")
        return event_id, event_type, payload

    def process(
        self, db: Session, fields: Mapping[str, str]
    ) -> CashSecurityOrderEventResult:
        event_id, event_type, payload = self._parse(fields)
        order_id = str(payload["order_id"]).strip()
        order = self.order_repository.get_by_order_id(db, order_id)
        exchange_id = str(payload["exchange_id"]).strip()
        symbol = str(payload["symbol"]).strip()
        if order is None:
            return CashSecurityOrderEventResult(
                event_id, event_type, order_id, exchange_id, symbol, "ORDER_NOT_FOUND"
            )
        if (
            order.instrument_type != "STOCK"
            or order.account_id != str(payload["account_id"]).strip()
            or order.exchange_id != exchange_id
            or order.symbol != symbol
        ):
            raise CashSecurityOrderEventError("现金证券订单事件与数据库事实不一致")
        should_remove = (
            event_type == self.CANCEL_EVENT
            or order.status not in self.ACTIVE_STATUSES
            or order.remaining_volume <= 0
        )
        if should_remove:
            self.active_order_index.remove_active_order(
                order_id=order.order_id,
                account_id=order.account_id,
                exchange_id=order.exchange_id,
                symbol=order.symbol,
                event_id=event_id,
                processed_ttl_seconds=self.processed_ttl_seconds,
            )
            return CashSecurityOrderEventResult(
                event_id, event_type, order_id, exchange_id, symbol,
                "REMOVED",
            )
        written = self.active_order_index.add_active_order(
            order,
            event_id=event_id,
            processed_ttl_seconds=self.processed_ttl_seconds,
        )
        return CashSecurityOrderEventResult(
            event_id, event_type, order_id, exchange_id, symbol,
            "REGISTERED" if written else "DUPLICATE",
        )
