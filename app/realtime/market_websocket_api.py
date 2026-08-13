import asyncio
import json
import logging
from time import monotonic
from uuid import uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.exceptions import RedisError

from app.common.exceptions import AppError
from app.common.time_utils import utc_now
from app.core.config import settings
from app.core.database import SessionLocal
from app.infrastructure.redis_keys import (
    MARKET_TICK_STREAM,
    YMM_LIVE_DATA_STATUS_KEY,
)
from app.realtime.connection_context import ConnectionContext
from app.realtime.event_enums import WebSocketCloseCode
from app.realtime.metrics import realtime_metrics
from app.realtime.websocket_api import _authenticate
from app.repositories.instrument_repository import InstrumentRepository


router = APIRouter()
logger = logging.getLogger(__name__)
instrument_repository = InstrumentRepository()


def _event(event_type: str, *, connection_id: str, payload: dict) -> str:
    """行情协议独立于交易事实Envelope，sequence只属于单合约行情。"""

    return json.dumps(
        {
            "event_id": f"{event_type}-{uuid4().hex}",
            "event_type": event_type,
            "connection_id": connection_id,
            "occurred_at": utc_now().isoformat(),
            "payload": payload,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _normalize_codes(message: dict) -> set[str]:
    values = message.get("order_book_ids")
    if not isinstance(values, list):
        raise ValueError("order_book_ids必须是数组")
    codes = {
        str(value).strip().upper()
        for value in values
        if str(value).strip()
    }
    if not codes:
        raise ValueError("order_book_ids不能为空")
    if any(len(code) > 64 for code in codes):
        raise ValueError("order_book_id长度超过限制")
    return codes


def _resolve_instruments(codes: set[str]):
    with SessionLocal() as db:
        instruments = instrument_repository.list_by_order_book_ids(db, codes)
        by_code = {item.order_book_id.upper(): item for item in instruments}
        missing = codes - set(by_code)
        inactive = {code for code, item in by_code.items() if not item.is_active}
        if missing:
            raise ValueError(f"合约不存在：{', '.join(sorted(missing))}")
        if inactive:
            raise ValueError(f"合约未启用：{', '.join(sorted(inactive))}")
        # ORM对象离开Session后不能延迟加载，因此只返回行情路由所需普通值。
        return {
            code: {
                "order_book_id": item.order_book_id,
                "exchange_id": item.exchange_id,
                "symbol": item.symbol,
            }
            for code, item in by_code.items()
        }


def _safe_source_status(runtime) -> dict:
    try:
        values = runtime.redis_client.hgetall(YMM_LIVE_DATA_STATUS_KEY)
    except RedisError:
        return {"status": "MARKET_UNAVAILABLE"}
    return {
        key: value
        for key, value in values.items()
        if key not in {"api_user", "api_token", "base_url", "url"}
    }


async def _enqueue(runtime, context, event_type: str, payload: dict) -> bool:
    return await runtime.manager.enqueue(
        context,
        _event(event_type, connection_id=context.connection_id, payload=payload),
    )


async def _publish_snapshots(runtime, context, instruments: dict) -> None:
    keys = {
        (item["exchange_id"], item["symbol"])
        for item in instruments.values()
    }
    snapshots = await asyncio.to_thread(runtime.market_tick_store.get_latest_many, keys)
    for item in instruments.values():
        values = snapshots.get((item["exchange_id"], item["symbol"]), {})
        if not values:
            await _enqueue(
                runtime,
                context,
                "MARKET_STATUS",
                {
                    "order_book_id": item["order_book_id"],
                    "status": "WAITING_MARKET_DATA",
                    "message": "已请求上游订阅，正在等待首条行情",
                },
            )
            continue
        public_values = {
            key: value
            for key, value in values.items()
            if key not in {"stream_message_id", "subscription_generation"}
        }
        await _enqueue(runtime, context, "MARKET_SNAPSHOT", public_values)


async def _market_stream_loop(runtime, context, subscriptions: set[str]) -> None:
    """从内部标准行情流只读消费，并按当前连接的合约集合过滤。"""

    cursor = "$"
    unavailable_reported = False
    while not context.closing:
        try:
            batches = await asyncio.to_thread(
                runtime.redis_client.xread,
                {MARKET_TICK_STREAM: cursor},
                count=100,
                block=1000,
            )
            unavailable_reported = False
            for _stream_name, rows in batches:
                for message_id, fields in rows:
                    cursor = message_id
                    code = str(fields.get("order_book_id", "")).upper()
                    if code not in subscriptions:
                        continue
                    try:
                        payload = json.loads(fields.get("payload", "{}"))
                    except (TypeError, json.JSONDecodeError):
                        logger.warning("标准行情Stream包含无效JSON message_id=%s", message_id)
                        continue
                    # Stream ID只作为服务端读取游标，绝不冒充合约行情sequence。
                    if not await _enqueue(runtime, context, "MARKET_UPDATE", payload):
                        return
        except RedisError:
            if not unavailable_reported:
                unavailable_reported = True
                await _enqueue(
                    runtime,
                    context,
                    "MARKET_STATUS",
                    {"status": "MARKET_UNAVAILABLE", "message": "行情服务暂不可用"},
                )
            await asyncio.sleep(1)


def _user_is_active(runtime, user_id: str) -> bool:
    with SessionLocal() as db:
        return runtime.auth_service.is_active(db, user_id)


async def _monitor(runtime, context, subscriptions: set[str]) -> None:
    last_auth_check = monotonic()
    renew_interval = max(
        1.0,
        settings.market_client_subscription_ttl_seconds / 3,
    )
    while not context.closing:
        await asyncio.sleep(min(settings.ws_heartbeat_interval_seconds, renew_interval))
        if context.closing:
            return
        if utc_now() >= context.token_expiration:
            await _enqueue(runtime, context, "AUTH_EXPIRED", {"reason": "ACCESS_TOKEN_EXPIRED"})
            await asyncio.sleep(0.05)
            await runtime.manager.close(
                context,
                code=WebSocketCloseCode.AUTH_EXPIRED,
                reason="Access Token已过期",
            )
            return
        if monotonic() - context.last_heartbeat_at > settings.ws_heartbeat_timeout_seconds:
            await runtime.manager.close(
                context,
                code=WebSocketCloseCode.HEARTBEAT_TIMEOUT,
                reason="应用层心跳超时",
            )
            return
        if subscriptions:
            try:
                await asyncio.to_thread(
                    runtime.client_market_subscription_store.request_codes,
                    connection_id=context.connection_id,
                    codes=set(subscriptions),
                )
            except RedisError:
                await _enqueue(
                    runtime,
                    context,
                    "MARKET_STATUS",
                    {"status": "MARKET_UNAVAILABLE", "message": "行情订阅续租失败"},
                )
        if monotonic() - last_auth_check >= settings.ws_auth_recheck_interval_seconds:
            active = await asyncio.to_thread(_user_is_active, runtime, context.user_id)
            if not active:
                await runtime.manager.close(
                    context,
                    code=WebSocketCloseCode.AUTHENTICATION_FAILED,
                    reason="当前用户不可用",
                )
                return
            last_auth_check = monotonic()
        await _enqueue(
            runtime,
            context,
            "HEARTBEAT",
            {"server_time": utc_now().isoformat()},
        )


@router.websocket("/ws/market")
async def market_websocket(websocket: WebSocket, ticket: str = ""):
    """认证后按需提供标准化行情；不向客户端暴露Redis或上游凭据。"""

    runtime = websocket.app.state.runtime
    if not runtime.active:
        await websocket.close(code=WebSocketCloseCode.SERVICE_UNAVAILABLE)
        return
    try:
        claims, identity, _authorized_ids = await asyncio.to_thread(
            _authenticate, runtime, ticket
        )
    except AppError:
        realtime_metrics.increment("market_ws_auth_failures")
        logger.warning("行情WebSocket连接认证失败")
        await websocket.close(
            code=WebSocketCloseCode.AUTHENTICATION_FAILED,
            reason="WebSocket认证失败",
        )
        return

    context = ConnectionContext(
        connection_id=uuid4().hex,
        websocket=websocket,
        user_id=identity.user_id,
        role=identity.role,
        token_jti=claims.token_jti,
        token_expiration=claims.token_expiration,
        connected_at=utc_now(),
        send_queue=asyncio.Queue(maxsize=settings.ws_send_queue_size),
    )
    await websocket.accept()
    if not await runtime.manager.register(context):
        await websocket.close(
            code=WebSocketCloseCode.LIMIT_EXCEEDED,
            reason="用户连接数量超过限制",
        )
        return

    subscriptions: set[str] = set()
    stream_task = asyncio.create_task(
        _market_stream_loop(runtime, context, subscriptions),
        name=f"market-stream-{context.connection_id}",
    )
    monitor_task = asyncio.create_task(
        _monitor(runtime, context, subscriptions),
        name=f"market-monitor-{context.connection_id}",
    )
    try:
        await _enqueue(
            runtime,
            context,
            "MARKET_STATUS",
            {"status": "CONNECTED", "source": await asyncio.to_thread(_safe_source_status, runtime)},
        )
        while not context.closing:
            message = await websocket.receive_json()
            action = str(message.get("action", "")).strip().lower()
            if action == "pong":
                context.last_heartbeat_at = monotonic()
                continue
            if action not in {"subscribe", "unsubscribe", "resync"}:
                await _enqueue(runtime, context, "ERROR", {
                    "error_code": "MARKET_ACTION_INVALID",
                    "message": "仅支持subscribe、unsubscribe、resync和pong",
                })
                continue
            try:
                codes = _normalize_codes(message)
                if action in {"subscribe", "resync"}:
                    if len(subscriptions | codes) > settings.market_client_subscription_max_codes_per_connection:
                        raise ValueError("当前连接的行情订阅数量超过限制")
                    instruments = await asyncio.to_thread(_resolve_instruments, codes)
                    if action == "subscribe":
                        expires_at = await asyncio.to_thread(
                            runtime.client_market_subscription_store.request_codes,
                            connection_id=context.connection_id,
                            codes=codes,
                        )
                        subscriptions.update(codes)
                        await _enqueue(runtime, context, "SUBSCRIPTION_STATUS", {
                            "action": "subscribed",
                            "order_book_ids": sorted(codes),
                            "active_order_book_ids": sorted(subscriptions),
                            "expires_at": expires_at.isoformat(),
                        })
                    elif not codes.issubset(subscriptions):
                        raise ValueError("只能重新同步当前连接已订阅的合约")
                    await _publish_snapshots(runtime, context, instruments)
                else:
                    await asyncio.to_thread(
                        runtime.client_market_subscription_store.remove_codes,
                        connection_id=context.connection_id,
                        codes=codes,
                    )
                    subscriptions.difference_update(codes)
                    await _enqueue(runtime, context, "SUBSCRIPTION_STATUS", {
                        "action": "unsubscribed",
                        "order_book_ids": sorted(codes),
                        "active_order_book_ids": sorted(subscriptions),
                    })
            except (AppError, RedisError, ValueError) as exc:
                await _enqueue(runtime, context, "ERROR", {
                    "error_code": getattr(exc, "error_code", "MARKET_SUBSCRIPTION_INVALID"),
                    "message": getattr(exc, "message", str(exc)),
                })
    except (WebSocketDisconnect, RuntimeError, json.JSONDecodeError):
        pass
    finally:
        stream_task.cancel()
        monitor_task.cancel()
        await asyncio.gather(stream_task, monitor_task, return_exceptions=True)
        try:
            await asyncio.to_thread(
                runtime.client_market_subscription_store.remove_connection,
                context.connection_id,
            )
        except RedisError:
            # TTL仍保证异常清理，不能因Redis短时故障阻塞WebSocket关闭。
            logger.warning("行情连接需求清理失败 connection_id=%s", context.connection_id)
        await runtime.manager.close(
            context,
            code=WebSocketCloseCode.NORMAL,
            reason="连接已关闭",
        )
