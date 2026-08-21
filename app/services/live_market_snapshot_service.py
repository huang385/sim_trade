from dataclasses import dataclass

from redis import Redis

from app.common.code_utils import normalize_code
from app.infrastructure.market_data.market_tick_store import MarketTickStore
from app.infrastructure.redis_keys import (
    YMM_LIVE_DATA_STATUS_KEY,
    market_latest_key,
)
from app.schemas.market_tick_schema import MarketTickIngestType
from app.services.market_tick_matching_service import ParsedMarketTickEvent


@dataclass(frozen=True)
class LiveMatchingEvent:
    """订单到达时可复用的一条当前WebSocket盘口事件。"""

    stream_message_id: str
    parsed_event: ParsedMarketTickEvent


class LiveMarketSnapshotService:
    """
    判断Redis最新行情是否属于当前可用的上游订阅。

    本服务不使用REST、不按行情年龄拒绝低活跃合约，也不推算交易日。它只
    检查行情源运行状态、合约订阅结果，且不再比较订阅代次：只要合约仍在
    当前订阅列表且行情源RUNNING，其最新盘口就视为该合约当前有效行情。
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
        """条件全部满足时返回撮合事件，否则安全等待下一条行情。"""

        normalized_exchange = normalize_code(exchange_id)
        normalized_symbol = normalize_code(symbol)
        pipeline = self.redis_client.pipeline(transaction=False)
        pipeline.hgetall(YMM_LIVE_DATA_STATUS_KEY)
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
        # 不再比较行情 Hash 上的订阅代次：合约仍在当前订阅列表、行情源
        # RUNNING 即说明它处于活跃订阅中；低活跃合约没有新 Tick 时，其
        # 最新盘口仍是上游最后推送的那条有效行情，可以直接用于委托定价
        # 和到达撮合。退订再重订的缝隙期由补取快照自然覆盖。
        if (
            latest.get("source"),
            latest.get("ingest_type"),
        ) not in {
            ("YMM_LIVE_DATA", MarketTickIngestType.LIVE_CALLBACK.value),
            ("YMM_DATA_SDK", MarketTickIngestType.REST_SNAPSHOT.value),
        }:
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
            parsed_event=ParsedMarketTickEvent(
                event_id=tick.source_event_id,
                exchange_id=tick.exchange_id,
                symbol=tick.symbol,
                tick=tick,
            ),
        )
