import json
from datetime import datetime

from app.common.time_utils import utc_now
from app.realtime.event_enums import RealtimeEventType
from app.realtime.event_schema import RealtimeEventEnvelope


SOURCE_EVENT_MAPPING = {
    "ORDER_ACCEPTED": RealtimeEventType.ORDER_CREATED,
    "ORDER_PARTIALLY_FILLED": RealtimeEventType.ORDER_UPDATED,
    "ORDER_FILLED": RealtimeEventType.ORDER_UPDATED,
    "ORDER_CANCELLED": RealtimeEventType.ORDER_CANCELLED,
    "ORDER_PARTIALLY_CANCELLED": RealtimeEventType.ORDER_CANCELLED,
    "TRADE_CREATED": RealtimeEventType.TRADE_CREATED,
}


class RealtimeEventProjectionService:
    """把已提交Outbox消息转换为不参与业务计算的统一绝对值事件。"""

    @staticmethod
    def project(
        *,
        source_message_id: str,
        fields: dict[str, str],
    ) -> RealtimeEventEnvelope:
        source_type = fields.get("event_type", "").strip()
        event_type = SOURCE_EVENT_MAPPING.get(source_type)
        if event_type is None:
            raise ValueError(f"不支持的实时投影事件: {source_type or '<empty>'}")
        try:
            payload = json.loads(fields.get("payload", ""))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("订单事件payload不是合法JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("订单事件payload必须是对象")
        event_id = str(fields.get("event_id") or payload.get("event_id") or "")
        account_id = str(payload.get("account_id") or "").strip()
        if not event_id or not account_id:
            raise ValueError("订单事件缺少event_id或account_id")
        if source_type == "ORDER_ACCEPTED":
            # 接单Outbox历史结构没有status字段，投影补充确定的绝对状态。
            payload = {**payload, "status": "ACCEPTED"}
        entity_id = str(
            payload.get("trade_id")
            if source_type == "TRADE_CREATED"
            else payload.get("order_id")
        ).strip()
        occurred_raw = (
            payload.get("updated_at")
            or payload.get("created_at")
            or payload.get("accepted_at")
            or payload.get("cancelled_at")
        )
        try:
            occurred_at = (
                datetime.fromisoformat(str(occurred_raw))
                if occurred_raw
                else utc_now()
            )
        except ValueError:
            occurred_at = utc_now()
        return RealtimeEventEnvelope(
            event_id=event_id,
            event_type=event_type,
            account_id=account_id,
            entity_id=entity_id or None,
            occurred_at=occurred_at,
            # Gateway路由时会以目标Stream消息编号覆盖该来源版本。
            version=source_message_id,
            payload=payload,
        )
