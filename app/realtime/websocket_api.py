import asyncio
import json
import logging
from time import monotonic
from uuid import uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.common.exceptions import AppError
from app.common.time_utils import utc_now
from app.core.config import settings
from app.core.database import SessionLocal
from app.realtime.connection_context import ConnectionContext
from app.realtime.event_enums import RealtimeEventType, WebSocketCloseCode
from app.realtime.event_schema import RealtimeEventEnvelope, SubscribeMessage
from app.realtime.metrics import realtime_metrics
from app.realtime.subscription_service import (
    RealtimeUserIdentity,
    SubscriptionAuthorization,
)


router = APIRouter()
logger = logging.getLogger(__name__)


def _control_event(
    event_type: RealtimeEventType,
    *,
    connection_id: str,
    payload: dict,
    version: str = "0-0",
) -> str:
    return RealtimeEventEnvelope(
        event_id=f"{event_type.value}-{uuid4().hex}",
        event_type=event_type,
        entity_id=connection_id,
        occurred_at=utc_now(),
        version=version,
        payload=payload,
    ).model_dump_json()


def _authenticate(runtime, ticket: str):
    claims = runtime.ticket_service.consume(ticket)
    with SessionLocal() as db:
        user = runtime.auth_service.authenticate(db, claims)
        accounts = runtime.authorization_service.list_accessible_accounts(
            db,
            user,
        )
        identity = RealtimeUserIdentity(
            user_id=user.user_id,
            role=user.role,
        )
        return claims, identity, frozenset(
            account.account_id for account in accounts
        )


def _authorize_subscription(
    runtime,
    *,
    user_id: str,
    account_ids: list[str],
    existing: set[str],
) -> SubscriptionAuthorization:
    with SessionLocal() as db:
        return runtime.subscription_service.authorize_current(
            db,
            user_id=user_id,
            requested_account_ids=account_ids,
            existing_account_ids=existing,
        )


def _recheck_subscriptions(runtime, context: ConnectionContext):
    with SessionLocal() as db:
        return runtime.subscription_service.recheck_current_subscriptions(
            db,
            user_id=context.user_id,
            subscribed_account_ids=set(context.subscribed_account_ids),
        )


def _build_snapshot(
    runtime,
    account_ids: set[str],
    identity: RealtimeUserIdentity,
) -> dict:
    with SessionLocal() as db:
        return runtime.snapshot_service.build(
            db,
            account_ids,
            identity=identity,
            require_realtime_consistency=True,
        )


async def _connection_monitor(runtime, context: ConnectionContext) -> None:
    """定时心跳、Token到期和用户禁用检查。"""

    last_auth_check = monotonic()
    while not context.closing:
        token_seconds = max(
            (context.token_expiration - utc_now()).total_seconds(),
            0.05,
        )
        auth_seconds = max(
            settings.ws_auth_recheck_interval_seconds
            - (monotonic() - last_auth_check),
            0.05,
        )
        await asyncio.sleep(
            min(
                settings.ws_heartbeat_interval_seconds,
                token_seconds,
                auth_seconds,
            )
        )
        if context.closing:
            return
        if utc_now() >= context.token_expiration:
            await runtime.manager.enqueue(
                context,
                _control_event(
                    RealtimeEventType.AUTH_EXPIRED,
                    connection_id=context.connection_id,
                    payload={"reason": "ACCESS_TOKEN_EXPIRED"},
                ),
            )
            await asyncio.sleep(0.05)
            await runtime.manager.close(
                context,
                code=WebSocketCloseCode.AUTH_EXPIRED,
                reason="Access Token已过期",
            )
            return
        if monotonic() - context.last_heartbeat_at > (
            settings.ws_heartbeat_timeout_seconds
        ):
            realtime_metrics.increment("ws_heartbeat_timeouts")
            await runtime.manager.close(
                context,
                code=WebSocketCloseCode.HEARTBEAT_TIMEOUT,
                reason="应用层心跳超时",
            )
            return
        if monotonic() - last_auth_check >= (
            settings.ws_auth_recheck_interval_seconds
        ):
            try:
                authorization = await asyncio.to_thread(
                    _recheck_subscriptions,
                    runtime,
                    context,
                )
            except AppError as exc:
                logger.warning(
                    "WebSocket当前用户已失效 connection_id=%s user_id=%s "
                    "error_code=%s",
                    context.connection_id,
                    context.user_id,
                    exc.error_code,
                )
                await runtime.manager.close(
                    context,
                    code=WebSocketCloseCode.AUTHENTICATION_FAILED,
                    reason="当前用户不可用",
                )
                return
            except Exception:
                logger.exception(
                    "WebSocket身份复查失败 connection_id=%s user_id=%s",
                    context.connection_id,
                    context.user_id,
                )
                await runtime.manager.close(
                    context,
                    code=WebSocketCloseCode.SERVICE_UNAVAILABLE,
                    reason="身份复查服务暂不可用",
                )
                return
            last_auth_check = monotonic()
            context.role = authorization.identity.role
            context.authorized_account_ids = authorization.account_ids
            revoked = (
                set(context.subscribed_account_ids)
                - set(authorization.account_ids)
            )
            if revoked:
                # 发送队列保存的是已序列化消息，无法可靠逐账户剔除。发现任一
                # 已订阅账户授权撤销后整条连接失败关闭，由close取消sender、
                # 丢弃队列并清理全部路由，客户端必须重新认证和加载快照。
                logger.warning(
                    "WebSocket订阅权限已撤销 connection_id=%s user_id=%s "
                    "account_count=%s",
                    context.connection_id,
                    context.user_id,
                    len(revoked),
                )
                await runtime.manager.close(
                    context,
                    code=WebSocketCloseCode.PERMISSION_DENIED,
                    reason="账户订阅权限已撤销，需要重新连接",
                )
                return
        await runtime.manager.enqueue(
            context,
            _control_event(
                RealtimeEventType.HEARTBEAT,
                connection_id=context.connection_id,
                payload={"server_time": utc_now().isoformat()},
            ),
        )


@router.websocket("/ws/trading")
async def trading_websocket(websocket: WebSocket, ticket: str = ""):
    """认证连接、处理账户订阅，并把业务事实以只读事件持续发送。"""

    runtime = websocket.app.state.runtime
    if not runtime.active:
        await websocket.close(
            code=WebSocketCloseCode.SERVICE_UNAVAILABLE,
            reason="Gateway当前不可用",
        )
        return
    try:
        claims, identity, authorized_ids = await asyncio.to_thread(
            _authenticate,
            runtime,
            ticket,
        )
        realtime_metrics.increment("ws_ticket_consumed")
        realtime_metrics.increment("ws_auth_success")
    except AppError:
        realtime_metrics.increment("ws_ticket_rejected")
        realtime_metrics.increment("ws_auth_failures")
        logger.warning("WebSocket连接认证失败")
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
        authorized_account_ids=authorized_ids,
        send_queue=asyncio.Queue(maxsize=settings.ws_send_queue_size),
    )
    await websocket.accept()
    if not await runtime.manager.register(context):
        await websocket.close(
            code=WebSocketCloseCode.LIMIT_EXCEEDED,
            reason="用户连接数量超过限制",
        )
        return
    logger.info(
        "WebSocket连接已建立 connection_id=%s user_id=%s",
        context.connection_id,
        context.user_id,
    )

    monitor_task = asyncio.create_task(
        _connection_monitor(runtime, context),
        name=f"ws-monitor-{context.connection_id}",
    )
    try:
        while not context.closing:
            message = await websocket.receive_json()
            action = str(message.get("action", "")).lower()
            if action == "pong":
                context.last_heartbeat_at = monotonic()
                continue
            try:
                request = SubscribeMessage.model_validate(message)
                if request.action == "unsubscribe":
                    targets = set(request.account_ids)
                    runtime.manager.unsubscribe(context, targets)
                    await runtime.manager.enqueue(
                        context,
                        _control_event(
                            RealtimeEventType.UNSUBSCRIBED,
                            connection_id=context.connection_id,
                            payload={"account_ids": sorted(targets)},
                        ),
                    )
                    continue

                realtime_metrics.increment("ws_subscription_requests")
                first_authorization = await asyncio.to_thread(
                    _authorize_subscription,
                    runtime,
                    user_id=context.user_id,
                    account_ids=request.account_ids,
                    existing=set(context.subscribed_account_ids),
                )
                # 授权仍每次查库，authorized_account_ids只是连接上下文审计值，
                # 绝不能用它绕过最新账户归属校验。
                context.role = first_authorization.identity.role
                target_set = set(first_authorization.account_ids)
                runtime.manager.subscribe(
                    context,
                    target_set,
                    snapshot_loading=True,
                )
                try:
                    cursor = await asyncio.to_thread(
                        runtime.event_store.current_cursor
                    )
                    snapshot = await asyncio.to_thread(
                        _build_snapshot,
                        runtime,
                        target_set,
                        first_authorization.identity,
                    )
                    # 快照构造完成后再次读取当前角色和账户归属，修复第一次
                    # 授权与快照发送之间账户转移的TOCTOU窗口。
                    second_authorization = await asyncio.to_thread(
                        _authorize_subscription,
                        runtime,
                        user_id=context.user_id,
                        account_ids=sorted(target_set),
                        existing=set(context.subscribed_account_ids),
                    )
                except Exception:
                    # 已注册路由后加载快照失败时不能继续假装同步。关闭连接
                    # 让客户端重连取得新快照，缓冲区会随连接一起清理。
                    logger.exception(
                        "WebSocket完整快照加载失败 connection_id=%s "
                        "user_id=%s",
                        context.connection_id,
                        context.user_id,
                    )
                    await runtime.manager.close(
                        context,
                        code=WebSocketCloseCode.RESYNC_REQUIRED,
                        reason="完整快照加载失败，需要重新同步",
                    )
                    return
                if set(second_authorization.account_ids) != target_set:
                    runtime.manager.unsubscribe(context, target_set)
                    await runtime.manager.close(
                        context,
                        code=WebSocketCloseCode.PERMISSION_DENIED,
                        reason="账户订阅权限已变化",
                    )
                    return
                context.role = second_authorization.identity.role
                context.authorized_account_ids = (
                    second_authorization.account_ids
                )
                snapshot.update(
                    {
                        "connection_id": context.connection_id,
                        "account_ids": sorted(target_set),
                        "event_cursor": cursor,
                    }
                )
                serialized = _control_event(
                    RealtimeEventType.SNAPSHOT,
                    connection_id=context.connection_id,
                    payload=snapshot,
                    version=cursor,
                )
                await runtime.manager.finish_snapshot(
                    context,
                    account_ids=target_set,
                    cursor=cursor,
                    snapshot_serialized=serialized,
                )
            except (AppError, ValidationError, ValueError) as exc:
                realtime_metrics.increment("ws_subscription_denied")
                logger.warning(
                    "WebSocket订阅被拒绝 connection_id=%s user_id=%s "
                    "error_code=%s",
                    context.connection_id,
                    context.user_id,
                    getattr(exc, "error_code", "WS_MESSAGE_INVALID"),
                )
                await runtime.manager.enqueue(
                    context,
                    _control_event(
                        RealtimeEventType.ERROR,
                        connection_id=context.connection_id,
                        payload={
                            "error_code": getattr(
                                exc,
                                "error_code",
                                "WS_MESSAGE_INVALID",
                            ),
                            "message": getattr(exc, "message", str(exc)),
                        },
                    ),
                )
    except (WebSocketDisconnect, RuntimeError, json.JSONDecodeError):
        pass
    finally:
        monitor_task.cancel()
        await runtime.manager.close(
            context,
            code=WebSocketCloseCode.NORMAL,
            reason="连接已关闭",
        )
