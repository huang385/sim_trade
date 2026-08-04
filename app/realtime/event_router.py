from app.realtime.connection_manager import ConnectionManager
from app.realtime.event_store import RealtimeEventStore
from app.realtime.metrics import realtime_metrics
from app.common.time_utils import utc_now


class RealtimeEventRouter:
    """解析一次、序列化一次，并按account_id路由到全部授权连接。"""

    def __init__(self, manager: ConnectionManager):
        self.manager = manager

    async def route(
        self,
        message_id: str,
        fields: dict[str, str],
    ) -> int:
        envelope = RealtimeEventStore.parse_fields(fields).model_copy(
            update={"version": message_id}
        )
        serialized = envelope.model_dump_json()
        realtime_metrics.increment("ws_events_received")
        latency_ms = max(
            int((utc_now() - envelope.occurred_at).total_seconds() * 1000),
            0,
        )
        realtime_metrics.set("ws_delivery_latency_ms", latency_ms)
        sent = await self.manager.broadcast_to_account(
            envelope,
            serialized,
        )
        if sent == 0:
            realtime_metrics.increment("ws_events_filtered")
        return sent
