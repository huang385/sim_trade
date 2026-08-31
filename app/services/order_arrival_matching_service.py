from dataclasses import dataclass

from app.matching.types import MatchingOrderCandidate
from app.schemas.matching_schema import MarketTickMatchResult
from app.services.live_market_snapshot_service import (
    LiveMarketSnapshotService,
)
from app.services.market_tick_matching_service import (
    MarketTickMatchingService,
)


@dataclass(frozen=True)
class OrderArrivalMatchResult:
    """订单到达即时撮合结果；无可用实时盘口时不视为异常。"""

    action: str
    matching_result: MarketTickMatchResult | None = None


class OrderArrivalMatchingService:
    """
    在订单进入活动索引后，用当前代实时WebSocket盘口尝试一次撮合。

    具体候选读取、价格判断、数据库行锁和成交结算全部复用行情撮合服务，
    本服务只负责判断当前缓存盘口能否安全用于订单到达触发。
    """

    def __init__(
        self,
        *,
        live_market_snapshot_service: LiveMarketSnapshotService,
        matching_service: MarketTickMatchingService,
    ):
        self.live_market_snapshot_service = (
            live_market_snapshot_service
        )
        self.matching_service = matching_service

    def match_if_ready(
        self,
        *,
        order_id: str,
        exchange_id: str,
        order_book_id: str,
        symbol: str,
        order_snapshot: MatchingOrderCandidate | None = None,
    ) -> OrderArrivalMatchResult:
        expected_bootstrap_stream_message_id = None
        if (
            order_snapshot is not None
            and order_snapshot.price_snapshot_source == "YMM_DATA_SDK"
            and order_snapshot.price_snapshot_stream_message_id
        ):
            expected_bootstrap_stream_message_id = (
                order_snapshot.price_snapshot_stream_message_id
            )
        event = self.live_market_snapshot_service.get_matching_event(
            exchange_id=exchange_id,
            order_book_id=order_book_id,
            symbol=symbol,
            allow_bootstrap_snapshot=(
                expected_bootstrap_stream_message_id is not None
            ),
            expected_bootstrap_stream_message_id=(
                expected_bootstrap_stream_message_id
            ),
        )
        if event is None:
            return OrderArrivalMatchResult(
                action="WAITING_FOR_LIVE_TICK"
            )

        result = self.matching_service.process_candidate_order(
            order_id=order_id,
            stream_message_id=event.stream_message_id,
            event=event.parsed_event,
            order_snapshot=order_snapshot,
        )
        return OrderArrivalMatchResult(
            action=(
                "SETTLED"
                if result.settled_count > 0
                else "CHECKED_NOT_MATCHED"
            ),
            matching_result=result,
        )
