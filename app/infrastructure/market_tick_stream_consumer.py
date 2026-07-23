from datetime import datetime
from typing import Mapping

from redis import Redis
from redis.exceptions import ResponseError

from app.common.time_utils import utc_now
from app.infrastructure.redis_keys import market_matching_failure_key


# Redis 5 的墓碑 Pending 可能返回空正文，Worker 仍需拿消息 ID 执行 ACK。
MarketStreamMessage = tuple[str, dict[str, str] | None]


class MarketTickStreamConsumer:
    """
    撮合行情Consumer Group、Pending、重试与死信的Redis适配器。

    本类只封装Redis命令，不解析行情、不查询数据库，也不判断订单能否成交。
    ACK时机由MatchingWorker根据业务处理结果决定。
    """

    def __init__(
        self,
        redis_client: Redis,
        *,
        stream_name: str,
        group_name: str,
        consumer_name: str,
        dead_letter_stream: str,
        failure_ttl_seconds: int,
    ):
        # 所有Key和消费参数都由配置或调用方传入，集成测试可使用隔离Stream，
        # 避免影响生产消费组的位置和Pending列表。
        self.redis_client = redis_client
        self.stream_name = stream_name
        self.group_name = group_name
        self.consumer_name = consumer_name
        self.dead_letter_stream = dead_letter_stream
        self.failure_ttl_seconds = failure_ttl_seconds

    def ensure_group(self) -> None:
        """
        从$创建新组；BUSYGROUP时绝不重置已有消费位置。

        $表示新建组不回放组创建前的历史行情。组已存在时只忽略BUSYGROUP，
        连接错误、权限错误等其他异常必须继续抛给Worker重试。
        """

        try:
            self.redis_client.xgroup_create(
                self.stream_name,
                self.group_name,
                id="$",
                mkstream=True,
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    @staticmethod
    def _flatten(result) -> list[MarketStreamMessage]:
        """把redis-py按Stream分组的结果整理成统一消息列表。"""

        if not result:
            return []
        messages: list[MarketStreamMessage] = []
        for _stream, rows in result:
            messages.extend(rows)
        return messages

    def read_new_messages(
        self, *, batch_size: int, block_ms: int
    ) -> list[MarketStreamMessage]:
        """
        通过XREADGROUP读取尚未分配给任何Consumer的新行情。

        BLOCK只是Redis服务端阻塞等待，新消息到达会立即唤醒，不是固定轮询
        延迟；无消息时到期返回空列表。
        """

        result = self.redis_client.xreadgroup(
            self.group_name,
            self.consumer_name,
            {self.stream_name: ">"},
            count=batch_size,
            block=block_ms,
        )
        return self._flatten(result)

    def claim_stale_messages(
        self, *, pending_idle_ms: int, batch_size: int
    ) -> list[MarketStreamMessage]:
        """Redis 6.2+ 使用 XAUTOCLAIM，Redis 5 自动回退到 XCLAIM。"""

        # 没有Pending时直接返回，避免无意义的XAUTOCLAIM往返。
        summary = self.redis_client.xpending(self.stream_name, self.group_name)
        pending_count = (
            summary.get("pending", 0)
            if isinstance(summary, Mapping)
            else summary[0]
        )
        if not pending_count:
            return []
        try:
            # Redis 6.2及以上优先使用单命令原子领取超时Pending。
            result = self.redis_client.xautoclaim(
                self.stream_name,
                self.group_name,
                self.consumer_name,
                pending_idle_ms,
                start_id="0-0",
                count=batch_size,
            )
            return list(result[1]) if result else []
        except ResponseError as exc:
            if "unknown command" not in str(exc).lower():
                raise

        # Redis 5没有XAUTOCLAIM：先找出空闲时间达到阈值的消息，再XCLAIM。
        pending_rows = self.redis_client.xpending_range(
            self.stream_name,
            self.group_name,
            min="-",
            max="+",
            count=batch_size,
        )
        message_ids = [
            row["message_id"]
            for row in pending_rows
            if row.get("time_since_delivered", 0) >= pending_idle_ms
        ]
        if not message_ids:
            return []
        claimed = list(
            self.redis_client.xclaim(
                self.stream_name,
                self.group_name,
                self.consumer_name,
                pending_idle_ms,
                message_ids,
            )
        )
        # 正文已被XDEL但PEL仍有记录时，Redis 5可能返回(None, None)。保留
        # XPENDING得到的原始ID，Worker才能XACK并清理这类墓碑。
        return [
            (returned_id or requested_id, fields)
            for requested_id, (returned_id, fields) in zip(message_ids, claimed)
        ]

    def acknowledge(self, message_id: str) -> int:
        """确认已经完整处理的行情消息并从消费组PEL中移除。"""

        return int(
            self.redis_client.xack(
                self.stream_name, self.group_name, message_id
            )
        )

    def increment_failure(self, message_id: str) -> int:
        """原子增加消息失败次数并刷新TTL，避免计数Key永久残留。"""

        key = market_matching_failure_key(message_id)
        pipeline = self.redis_client.pipeline(transaction=True)
        pipeline.incr(key)
        pipeline.expire(key, self.failure_ttl_seconds)
        return int(pipeline.execute()[0])

    def clear_failure(self, message_id: str) -> None:
        """消息ACK或成功进入死信后删除失败次数。"""

        self.redis_client.delete(market_matching_failure_key(message_id))

    def publish_dead_letter(
        self,
        *,
        source_message_id: str,
        fields: Mapping[str, str],
        error: str,
        failed_at: datetime | None = None,
    ) -> str:
        """
        发布死信并返回死信消息编号。

        本方法不ACK原消息。Worker必须严格执行“先写死信、后ACK”，这样Redis
        暂时不可用时不会既丢失原消息又没有留下死信记录。
        """

        message_id = self.redis_client.xadd(
            self.dead_letter_stream,
            fields={
                "source_stream": self.stream_name,
                "source_message_id": source_message_id,
                "event_id": fields.get("event_id", ""),
                "event_type": fields.get("event_type", ""),
                "exchange_id": fields.get("exchange_id", ""),
                "symbol": fields.get("symbol", ""),
                "payload": fields.get("payload", ""),
                "error": error[:4000],
                "failed_at": (failed_at or utc_now()).isoformat(),
                "consumer_name": self.consumer_name,
            },
        )
        return message_id.decode() if isinstance(message_id, bytes) else str(message_id)
