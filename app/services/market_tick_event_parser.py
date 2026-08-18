"""Product-neutral validation for a market-tick stream event."""

import json
from dataclasses import dataclass
from typing import Mapping

from app.schemas.market_tick_schema import MarketTick, MarketTickIngestType


class MarketTickEventValidationError(ValueError):
    """The stream message is structurally invalid and cannot succeed on retry."""


class UnsupportedMarketTickEventError(MarketTickEventValidationError):
    """The stream message is not an executable real-time tick."""


@dataclass(frozen=True)
class ParsedMarketTickEvent:
    event_id: str
    exchange_id: str
    symbol: str
    tick: MarketTick


def parse_market_tick_event(fields: Mapping[str, str]) -> ParsedMarketTickEvent:
    event_id = str(fields.get("event_id", "")).strip()
    event_type = str(fields.get("event_type", "")).strip()
    if not event_id:
        raise MarketTickEventValidationError("行情事件缺少 event_id")
    if event_type != "MARKET_TICK":
        raise UnsupportedMarketTickEventError(
            f"不支持的行情事件类型: {event_type or '<empty>'}"
        )
    try:
        payload = json.loads(fields.get("payload", ""))
    except (TypeError, json.JSONDecodeError) as exc:
        raise MarketTickEventValidationError("行情 payload 不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise MarketTickEventValidationError("行情 payload 必须是 JSON 对象")
    if (payload.get("source"), payload.get("ingest_type")) not in {
        ("YMM_LIVE_DATA", MarketTickIngestType.LIVE_CALLBACK.value),
        ("YMM_DATA_SDK", MarketTickIngestType.REST_SNAPSHOT.value),
    }:
        raise UnsupportedMarketTickEventError("不支持的行情来源或接入类型")
    try:
        tick = MarketTick.model_validate(payload)
    except Exception as exc:
        raise MarketTickEventValidationError("行情 payload 字段不合法") from exc
    if tick.source_event_id != event_id:
        raise MarketTickEventValidationError("event_id 与 payload 不一致")
    exchange_id = str(fields.get("exchange_id", "")).strip()
    symbol = str(fields.get("symbol", "")).strip()
    if exchange_id != tick.exchange_id or symbol != tick.symbol:
        raise MarketTickEventValidationError("行情路由字段与 payload 不一致")
    return ParsedMarketTickEvent(event_id, exchange_id, symbol, tick)
