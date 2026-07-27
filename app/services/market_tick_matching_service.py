import json
from dataclasses import dataclass
from typing import Callable, Mapping

from sqlalchemy.orm import Session

from app.enums.order_enums import (
    OffsetFlag,
    OrderDirection,
    OrderStatus,
    OrderType,
)
from app.infrastructure.active_order_index import ActiveOrderIndex
from app.matching.base import MatchingEngine
from app.matching.models import MatchingMarketData, MatchingOrder
from app.repositories.order_repository import OrderRepository
from app.schemas.market_tick_schema import MarketTick, MarketTickIngestType
from app.schemas.matching_schema import MarketTickMatchResult
from app.services.trade_settlement_service import (
    SettlementCommand,
    TradeSettlementService,
)


class MarketTickEventValidationError(ValueError):
    """行情 Stream 消息格式错误，重试不会自动修复。"""


class UnsupportedMarketTickEventError(MarketTickEventValidationError):
    """非实时行情事件，不能触发撮合。"""


@dataclass(frozen=True)
class ParsedMarketTickEvent:
    """通过基础格式和来源检查后的实时行情事件。"""

    # 行情源事件编号
    event_id: str
    # 用于路由活动订单Set的交易所和合约代码
    exchange_id: str
    symbol: str
    # Pydantic完成类型转换后的标准行情对象
    tick: MarketTick


class MarketTickMatchingService:
    """
    协调候选订单读取、纯撮合和逐订单独立成交事务。

    本服务是Redis候选索引、纯撮合引擎和PostgreSQL结算服务之间的编排层。
    它不直接修改ORM对象，也不负责ACK；只有全部候选订单都达到确定结果后，
    外层MatchingWorker才会确认该行情消息。
    """

    ACTIVE_STATUSES = {
        OrderStatus.ACCEPTED.value,
        OrderStatus.PARTIALLY_FILLED.value,
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
        session_factory: Callable[[], Session],
        active_order_index: ActiveOrderIndex,
        order_repository: OrderRepository,
        matching_engine: MatchingEngine,
        settlement_service: TradeSettlementService,
    ):
        # Session工厂而不是单个Session被注入，因为每笔候选订单必须使用
        # 独立事务，才能让一笔失败不阻止其他订单先完成结算。
        self.session_factory = session_factory
        self.active_order_index = active_order_index
        self.order_repository = order_repository
        self.matching_engine = matching_engine
        self.settlement_service = settlement_service

    @staticmethod
    def parse_event(fields: Mapping[str, str]) -> ParsedMarketTickEvent:
        """
        解析Stream消息，只允许优美利实时回调进入撮合。

        格式、来源或接入方式不合法属于永久消息错误，Worker会直接尝试写入
        死信；数据库临时异常则属于可恢复错误，必须保留Pending重试。
        """

        # 顶层字段用于Consumer快速路由，payload保存完整标准行情快照。
        event_id = str(fields.get("event_id", "")).strip()
        event_type = str(fields.get("event_type", "")).strip()
        if not event_id:
            raise MarketTickEventValidationError("行情事件缺少event_id")
        if event_type != "MARKET_TICK":
            raise UnsupportedMarketTickEventError(
                f"不支持的行情事件类型: {event_type or '<empty>'}"
            )
        try:
            # Stream payload由行情发布端序列化为JSON；禁止使用eval等方式解析。
            payload = json.loads(fields.get("payload", ""))
        except (TypeError, json.JSONDecodeError) as exc:
            raise MarketTickEventValidationError("行情payload不是合法JSON") from exc
        if not isinstance(payload, dict):
            raise MarketTickEventValidationError("行情payload必须是JSON对象")
        if payload.get("source") != "YML_FEEDHUB":
            raise UnsupportedMarketTickEventError("只允许YML_FEEDHUB行情撮合")
        if payload.get("ingest_type") != MarketTickIngestType.LIVE_CALLBACK.value:
            raise UnsupportedMarketTickEventError("非LIVE_CALLBACK行情不触发撮合")
        try:
            # Pydantic负责把日期、时间、整数和Decimal字符串转换为正确类型。
            tick = MarketTick.model_validate(payload)
        except Exception as exc:
            raise MarketTickEventValidationError("行情payload字段不合法") from exc
        if tick.source_event_id != event_id:
            # 顶层事件编号和payload不一致时无法建立可靠成交幂等键。
            raise MarketTickEventValidationError("event_id与payload不一致")
        exchange_id = str(fields.get("exchange_id", "")).strip()
        symbol = str(fields.get("symbol", "")).strip()
        if exchange_id != tick.exchange_id or symbol != tick.symbol:
            raise MarketTickEventValidationError("行情路由字段与payload不一致")
        return ParsedMarketTickEvent(event_id, exchange_id, symbol, tick)

    @classmethod
    def _database_order_is_candidate(cls, order, event: ParsedMarketTickEvent) -> bool:
        """Redis 只提供候选编号，是否活动必须以 PostgreSQL 为准。"""

        return (
            order is not None
            and order.status in cls.ACTIVE_STATUSES
            and order.remaining_volume > 0
            and order.order_type == OrderType.LIMIT.value
            and order.offset_flag in cls.SUPPORTED_OFFSET_FLAGS
            and order.exchange_id == event.exchange_id
            and order.symbol == event.symbol
        )

    def process(
        self,
        *,
        stream_message_id: str,
        fields: Mapping[str, str],
    ) -> MarketTickMatchResult:
        """
        处理一条Tick；任一暂时性数据库错误最终会使整条消息重试。

        明确不成交、数据库确认订单失效、成交成功和幂等成功都属于可ACK结果。
        只有暂时性异常会在处理完其余候选后重新抛出，使原Tick保留Pending。
        """

        event = self.parse_event(fields)
        # Redis只保存可重建的活动订单派生索引，用于缩小数据库查询范围。
        # 它不能证明订单仍然有效，后续必须查询PostgreSQL再次确认。
        order_ids = sorted(
            self.active_order_index.list_instrument_order_ids(
                event.exchange_id, event.symbol
            )
        )
        # 同一条 Tick 的所有候选订单共享一份不可变行情快照，避免在高频
        # 循环中重复构造对象，同时仍保持 VN 模式下盘口量互不扣减的语义。
        market_snapshot = MatchingMarketData(
            bid_price_1=event.tick.bid_price_1,
            bid_volume_1=event.tick.bid_volume_1,
            ask_price_1=event.tick.ask_price_1,
            ask_volume_1=event.tick.ask_volume_1,
        )
        matched = settled = idempotent = skipped = 0
        first_error: Exception | None = None

        for order_id in order_ids:
            try:
                # 预读只用于生成引擎输入；真正写入前 SettlementService 会再次
                # SELECT FOR UPDATE 并按数据库最新状态校验和收缩成交量。
                with self.session_factory() as read_db:
                    order = self.order_repository.get_by_order_id(read_db, order_id)
                    if not self._database_order_is_candidate(order, event):
                        skipped += 1
                        continue
                    order_snapshot = MatchingOrder(
                        direction=OrderDirection(order.direction),
                        offset_flag=OffsetFlag(order.offset_flag),
                        order_type=OrderType(order.order_type),
                        limit_price=order.limit_price,
                        remaining_volume=order.remaining_volume,
                    )
                match_result = self.matching_engine.match(
                    order_snapshot,
                    market_snapshot,
                )
                if not match_result.matched:
                    # 价格未触达或盘口量为0是正常结果，不需要重试该Tick。
                    skipped += 1
                    continue
                matched += 1
                # 纯撮合结果不携带订单编号或 Redis 信息；编排层在确认成交后
                # 组合结算命令，确保幂等和 Trade 追踪字段仍完整保留。
                command = SettlementCommand(
                    order_id=order.order_id,
                    market_event_id=event.event_id,
                    market_stream_message_id=stream_message_id,
                    tick_event_time=event.tick.event_time,
                    tick_sequence_id=event.tick.sequence_id,
                    match_result=match_result,
                )
                # 每笔候选订单使用独立 Session 和事务。一笔失败不妨碍后续
                # 订单先完成；但循环结束后仍抛错，使整条 Tick 保留在 Pending。
                with self.session_factory() as settlement_db:
                    result = self.settlement_service.settle(
                        settlement_db, command
                    )
                if result.action == "SETTLED":
                    settled += 1
                elif result.action == "IDEMPOTENT":
                    # 表示该订单已经被同一行情成功结算过，重试可安全继续。
                    idempotent += 1
                else:
                    skipped += 1
            except Exception as exc:
                # 记录第一个异常但不中断循环，使同一Tick的其他独立订单仍有
                # 机会完成。循环结束重新抛出后，Worker不会ACK原行情。
                if first_error is None:
                    first_error = exc

        if first_error is not None:
            raise first_error
        return MarketTickMatchResult(
            candidate_count=len(order_ids),
            matched_count=matched,
            settled_count=settled,
            idempotent_count=idempotent,
            skipped_count=skipped,
        )
