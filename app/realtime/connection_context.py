import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from time import monotonic

from fastapi import WebSocket


@dataclass
class ConnectionContext:
    """一条WebSocket连接持有的最小身份、订阅和发送状态。"""

    connection_id: str
    websocket: WebSocket
    user_id: str
    role: str
    token_jti: str
    token_expiration: datetime
    connected_at: datetime
    send_queue: asyncio.Queue[str]
    authorized_account_ids: frozenset[str] = frozenset()
    subscribed_account_ids: set[str] = field(default_factory=set)
    snapshot_loading_accounts: set[str] = field(default_factory=set)
    snapshot_buffers: dict[str, list[tuple[str, str]]] = field(
        default_factory=dict
    )
    last_versions: dict[str, str] = field(default_factory=dict)
    last_heartbeat_at: float = field(default_factory=monotonic)
    sender_task: asyncio.Task | None = None
    closing: bool = False
