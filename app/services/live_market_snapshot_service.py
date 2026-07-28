from dataclasses import dataclass

from redis import Redis

from app.common.code_utils import normalize_code
from app.infrastructure.market_data.market_tick_store import MarketTickStore
from app.infrastructure.redis_keys import (
    YML_FEEDHUB_STATUS_KEY,
    market_latest_key,
)
from app.schemas.market_tick_schema import MarketTickIngestType


@dataclass(frozen=True)
class LiveMatchingEvent:
    """订单到达时可复用的一条当前WebSocket盘口事件。"""

    stream_message_id: str
    fields: dict[str, str]


class LiveMarketSnapshotService:
    """
    判断Redis最新行情是否属于当前可用的WebSocket订阅。

    本服务不使用REST、不按行情年龄拒绝低活跃合约，也不推算交易日。它只
    检查行情源运行状态、合约订阅结果和当前订阅代次是否已经收到真实Tick。
    """

    def __init__(self, redis_client: Redis):
        self.redis_client = redis_client

    @staticmethod
    def _subscribed_codes(status: dict[str, str]) -> set[str]:
        return {
            item.strip().upper()
            for item in status.get("subscribed_codes", "").split(",")
            if item.strip()
        }

    def get_matching_event(
        self,
        *,
        exchange_id: str,
        symbol: str,
    ) -> LiveMatchingEvent | None:
        """条件全部满足时返回撮合事件，否则安全等待下一条实时Tick。"""

        normalized_exchange = normalize_code(exchange_id)
        normalized_symbol = normalize_code(symbol)
        pipeline = self.redis_client.pipeline(transaction=False)
        pipeline.hgetall(YML_FEEDHUB_STATUS_KEY)
        pipeline.hgetall(
            market_latest_key(
                normalized_exchange,
                normalized_symbol,
            )
        )
        status, latest = pipeline.execute()

        if status.get("status") != "RUNNING":
            return None
        if normalized_symbol not in self._subscribed_codes(status):
            return None

        current_generation = status.get(
            "subscription_generation",
            "",
        )
        if (
            not current_generation
            or latest.get("subscription_generation")
            != current_generation
        ):
            return None
        if (
            latest.get("source") != "YML_FEEDHUB"
            or latest.get("ingest_type")
            != MarketTickIngestType.LIVE_CALLBACK.value
        ):
            return None

        stream_message_id = latest.get("stream_message_id", "").strip()
        if not stream_message_id:
            return None
        try:
            tick = MarketTickStore.mapping_to_tick(latest)
        except Exception:
            return None
        if (
            tick.exchange_id != normalized_exchange
            or tick.symbol != normalized_symbol
        ):
            return None

        return LiveMatchingEvent(
            stream_message_id=stream_message_id,
            fields={
                "event_id": tick.source_event_id,
                "event_type": MarketTickStore.EVENT_TYPE,
                "exchange_id": tick.exchange_id,
                "symbol": tick.symbol,
                "order_book_id": tick.order_book_id,
                "payload": MarketTickStore.tick_to_payload(tick),
            },
        )
