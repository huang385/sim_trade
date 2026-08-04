from datetime import datetime
from typing import Callable, Mapping

from redis import Redis
from redis.exceptions import ResponseError

from app.common.time_utils import utc_now
from app.infrastructure.redis_keys import order_event_failure_key


# Redis 5 在 Pending 消息正文已被 XDEL 后，XCLAIM 会返回 (message_id, None)。
# 保留 None 交给 Worker 执行 XACK，才能清除这类墓碑 Pending。
StreamMessage = tuple[str, dict[str, str] | None]


class OrderStreamConsumer:
    """
    Redis Stream Consumer Group 操作适配器。

    本类封装 Group 创建、新消息读取、Pending 恢复、ACK、失败计数和死信
    发布，不访问 PostgreSQL，也不包含任何订单状态判断。
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
        failure_key_factory: Callable[[str], str] = (
            order_event_failure_key
        ),
        group_start_id: str = "0-0",
    ):
        self.redis_client = redis_client
        self.stream_name = stream_name
        self.group_name = group_name
        self.consumer_name = consumer_name
        self.dead_letter_stream = dead_letter_stream
        self.failure_ttl_seconds = failure_ttl_seconds
        self.failure_key_factory = failure_key_factory
        self.group_start_id = group_start_id

    def ensure_group(self) -> None:
        """
        使用配置的起始游标创建消费组，已存在时安全忽略 BUSYGROUP。

        通用订单消费者默认0-0以读取历史事件；只需新事件的投影消费者可
        使用$。BUSYGROUP分支不会重置已有消费组的位置。
        """

        try:
            self.redis_client.xgroup_create(
                self.stream_name,
                self.group_name,
                id=self.group_start_id,
                mkstream=True,
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    @staticmethod
    def _flatten_stream_result(result) -> list[StreamMessage]:
        """把 redis-py 的多 Stream 返回结构整理成消息列表。"""

        if not result:
            return []
        messages: list[StreamMessage] = []
        for _stream_name, stream_messages in result:
            messages.extend(stream_messages)
        return messages

    def read_new_messages(
        self,
        *,
        batch_size: int,
        block_ms: int,
    ) -> list[StreamMessage]:
        """通过 XREADGROUP 读取从未投递过的新消息。"""

        result = self.redis_client.xreadgroup(
            self.group_name,
            self.consumer_name,
            {self.stream_name: ">"},
            count=batch_size,
            block=block_ms,
        )
        return self._flatten_stream_result(result)

    def claim_stale_messages(
        self,
        *,
        pending_idle_ms: int,
        batch_size: int,
    ) -> list[StreamMessage]:
        """
        重新领取崩溃 Consumer 遗留的超时 Pending 消息。

        Redis 6.2+ 优先使用 XAUTOCLAIM。本项目本机 Redis 5.x 不支持该命令，
        因此遇到 unknown command 时通过 XPENDING + XCLAIM 提供等价兼容路径。
        """

        summary = self.redis_client.xpending(
            self.stream_name,
            self.group_name,
        )
        pending_count = (
            summary.get("pending", 0)
            if isinstance(summary, Mapping)
            else summary[0]
        )
        if not pending_count:
            return []

        try:
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

        # Redis 5 兼容：先查看 Pending 的空闲时长，再领取符合条件的消息。
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
        claimed_messages = list(
            self.redis_client.xclaim(
                self.stream_name,
                self.group_name,
                self.consumer_name,
                pending_idle_ms,
                message_ids,
            )
        )
        # Redis 5 + redis-py 8 对已XDEL的PEL条目返回(None, None)，消息ID
        # 只能从前一步XPENDING保留。按XCLAIM请求顺序补回ID，Worker才能XACK。
        normalized_messages: list[StreamMessage] = []
        for requested_id, claimed_message in zip(
            message_ids,
            claimed_messages,
        ):
            returned_id, fields = claimed_message
            normalized_messages.append(
                (returned_id or requested_id, fields)
            )
        return normalized_messages

    def acknowledge(self, message_id: str) -> int:
        """确认一条已经完整处理的消息。"""

        return int(
            self.redis_client.xack(
                self.stream_name,
                self.group_name,
                message_id,
            )
        )

    def increment_failure(self, message_id: str) -> int:
        """增加失败次数并刷新 TTL，避免计数键永久占用 Redis。"""

        key = self.failure_key_factory(message_id)
        pipeline = self.redis_client.pipeline(transaction=True)
        pipeline.incr(key)
        pipeline.expire(key, self.failure_ttl_seconds)
        results = pipeline.execute()
        return int(results[0])

    def clear_failure(self, message_id: str) -> None:
        """消息成功处理或进入死信后删除失败计数。"""

        self.redis_client.delete(self.failure_key_factory(message_id))

    def publish_dead_letter(
        self,
        *,
        source_message_id: str,
        fields: Mapping[str, str],
        error: str,
        failed_at: datetime | None = None,
    ) -> str:
        """把无法处理的原消息写入死信 Stream，并返回新消息编号。"""

        message_id = self.redis_client.xadd(
            self.dead_letter_stream,
            fields={
                "source_stream": self.stream_name,
                "source_message_id": source_message_id,
                "event_id": fields.get("event_id", ""),
                "event_type": fields.get("event_type", ""),
                "payload": fields.get("payload", ""),
                "error": error,
                "failed_at": (failed_at or utc_now()).isoformat(),
                "consumer_name": self.consumer_name,
            },
        )
        if isinstance(message_id, bytes):
            return message_id.decode("utf-8")
        return str(message_id)
