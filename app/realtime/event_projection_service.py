import json
from datetime import datetime

from app.common.time_utils import utc_now
from app.realtime.event_enums import RealtimeEventType
from app.realtime.event_schema import RealtimeEventEnvelope


SOURCE_EVENT_MAPPING = {
    "ORDER_ACCEPTED": RealtimeEventType.ORDER_CREATED,
    "ORDER_PARTIALLY_FILLED": RealtimeEventType.ORDER_UPDATED,
    "ORDER_FILLED": RealtimeEventType.ORDER_UPDATED,
    "ORDER_MARGIN_UPDATED": RealtimeEventType.ORDER_UPDATED,
    "ORDER_CANCELLED": RealtimeEventType.ORDER_CANCELLED,
    "ORDER_PARTIALLY_CANCELLED": RealtimeEventType.ORDER_CANCELLED,
    "TRADE_CREATED": RealtimeEventType.TRADE_CREATED,
    "POSITION_UPDATED": RealtimeEventType.POSITION_UPDATED,
    "POSITION_CLOSED": RealtimeEventType.POSITION_CLOSED,
    "ACCOUNT_FACT_UPDATED": RealtimeEventType.ACCOUNT_FACT_UPDATED,
    # 兼容升级前数据库中尚未发布的旧账户事实Outbox。
    "ACCOUNT_UPDATED": RealtimeEventType.ACCOUNT_FACT_UPDATED,
    "RISK_WARNING": RealtimeEventType.RISK_WARNING,
    "RISK_STATE_CHANGED": RealtimeEventType.RISK_STATE_CHANGED,
    "LIQUIDATION_STARTED": RealtimeEventType.LIQUIDATION_STARTED,
    "LIQUIDATION_ORDER_UPDATED": RealtimeEventType.LIQUIDATION_ORDER_UPDATED,
    "LIQUIDATION_COMPLETED": RealtimeEventType.LIQUIDATION_COMPLETED,
    "LIQUIDATION_FAILED": RealtimeEventType.LIQUIDATION_FAILED,
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
        aggregate_type = str(fields.get("aggregate_type") or "").strip().upper()
        aggregate_id = str(fields.get("aggregate_id") or "").strip()
        business_version = str(fields.get("business_version") or "").strip()
        if aggregate_type == "RISK":
            # 风险聚合使用Account.risk_version，而不是Outbox全局主键；同一账户
            # 的状态版本连续单调，客户端和Redis投影可可靠拒绝迟到旧事件。
            business_version = str(payload.get("risk_version") or "").strip()
        account_id = str(payload.get("account_id") or "").strip()
        if not event_id or not account_id:
            raise ValueError("订单事件缺少event_id或account_id")
        if source_type == "ORDER_ACCEPTED":
            # 接单Outbox历史结构没有status字段，投影补充确定的绝对状态。
            payload = {**payload, "status": "ACCEPTED"}
        if not aggregate_type or not aggregate_id or not business_version.isdigit():
            raise ValueError("实时投影事件缺少合法聚合根业务版本")
        if source_type == "TRADE_CREATED":
            entity_id = str(payload.get("trade_id") or "").strip()
        elif source_type in {"POSITION_UPDATED", "POSITION_CLOSED"}:
            entity_id = str(payload.get("position_id") or "").strip()
        elif source_type in {"ACCOUNT_FACT_UPDATED", "ACCOUNT_UPDATED"}:
            entity_id = account_id
        elif aggregate_type == "RISK":
            entity_id = str(payload.get("task_id") or account_id).strip()
        else:
            entity_id = str(payload.get("order_id") or "").strip()
        # 客户端收到的绝对事实中直接携带业务版本，便于独立于传输游标
        # 检查同一订单、持仓或账户是否发生状态倒退。
        payload = {**payload, "business_version": business_version}
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
            business_version=business_version,
            payload=payload,
        )
