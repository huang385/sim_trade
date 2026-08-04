import json

from redis import Redis

from app.core.config import settings
from app.infrastructure.redis_keys import (
    REALTIME_EVENT_STREAM,
    projected_realtime_event_key,
    realtime_aggregate_business_version_key,
)
from app.realtime.event_schema import RealtimeEventEnvelope


PUBLISH_PROJECTED_EVENT_ONCE_SCRIPT = """
if redis.call('EXISTS', KEYS[2]) == 1 then
    return ''
end

-- 业务版本是PostgreSQL正整数主键。使用字符串长度和字典序比较，避免Lua
-- double在大整数超过2^53后丢失精度。
local function normalize_integer(value)
    local normalized = string.gsub(value or '', '^0+', '')
    if normalized == '' then
        return '0'
    end
    return normalized
end

local function is_greater(left, right)
    left = normalize_integer(left)
    right = normalize_integer(right)
    if string.len(left) ~= string.len(right) then
        return string.len(left) > string.len(right)
    end
    return left > right
end

local current_version = redis.call('GET', KEYS[3])
if current_version and not is_greater(ARGV[8], current_version) then
    -- 迟到旧事件也标记为已处理，避免至少一次投递反复触发相同比较。
    redis.call('SET', KEYS[2], 'STALE', 'EX', ARGV[6])
    return 'STALE'
end
local message_id = redis.call(
    'XADD', KEYS[1], 'MAXLEN', '~', ARGV[7], '*',
    'event_id', ARGV[1],
    'event_type', ARGV[2],
    'account_id', ARGV[3],
    'entity_id', ARGV[4],
    'payload', ARGV[5]
)
redis.call('SET', KEYS[3], ARGV[8])
redis.call('SET', KEYS[2], message_id, 'EX', ARGV[6])
return message_id
"""


class RealtimeEventStore:
    """统一实时事件Stream的发布、幂等投影和游标查询适配器。"""

    def __init__(
        self,
        redis_client: Redis,
        *,
        stream_name: str = REALTIME_EVENT_STREAM,
    ):
        self.redis_client = redis_client
        self.stream_name = stream_name

    @staticmethod
    def serialize(envelope: RealtimeEventEnvelope) -> str:
        return envelope.model_dump_json()

    def publish(self, envelope: RealtimeEventEnvelope) -> str:
        message_id = self.redis_client.xadd(
            self.stream_name,
            fields={
                "event_id": envelope.event_id,
                "event_type": envelope.event_type.value,
                "account_id": envelope.account_id or "",
                "entity_id": envelope.entity_id or "",
                "payload": self.serialize(envelope),
            },
            maxlen=settings.realtime_event_stream_maxlen,
            approximate=True,
        )
        return (
            message_id.decode("utf-8")
            if isinstance(message_id, bytes)
            else str(message_id)
        )

    def publish_projected_once(
        self,
        envelope: RealtimeEventEnvelope,
        *,
        processed_ttl_seconds: int | None = None,
    ) -> str | None:
        """用Lua原子执行幂等检查、XADD和标记，防止Outbox重放重复。"""

        ttl = (
            processed_ttl_seconds
            if processed_ttl_seconds is not None
            else settings.order_event_processed_ttl_seconds
        )
        aggregate_type = str(envelope.payload.get("aggregate_type") or "")
        aggregate_id = str(envelope.payload.get("aggregate_id") or "")
        # 旧payload没有聚合字段时，从事件实体类型和编号恢复；投影服务会
        # 对Outbox输入做严格校验，这里的回退仅方便直接构造Envelope的测试。
        if not aggregate_type:
            aggregate_type = (
                "ACCOUNT"
                if envelope.event_type.value
                in {"ACCOUNT_FACT_UPDATED", "ACCOUNT_UPDATED"}
                else "POSITION"
                if envelope.event_type.value in {"POSITION_UPDATED", "POSITION_CLOSED"}
                else "TRADE"
                if envelope.event_type.value == "TRADE_CREATED"
                else "ORDER"
            )
        aggregate_id = aggregate_id or envelope.entity_id or envelope.account_id or ""
        business_version = str(envelope.business_version or "")
        if not aggregate_id or not business_version.isdigit():
            raise ValueError("实时投影缺少合法聚合根业务版本")
        result = self.redis_client.eval(
            PUBLISH_PROJECTED_EVENT_ONCE_SCRIPT,
            3,
            self.stream_name,
            projected_realtime_event_key(envelope.event_id),
            realtime_aggregate_business_version_key(
                aggregate_type,
                aggregate_id,
            ),
            envelope.event_id,
            envelope.event_type.value,
            envelope.account_id or "",
            envelope.entity_id or "",
            self.serialize(envelope),
            ttl,
            settings.realtime_event_stream_maxlen,
            business_version,
        )
        if not result or result in {"STALE", b"STALE"}:
            return None
        return result.decode() if isinstance(result, bytes) else str(result)

    def current_cursor(self) -> str:
        """读取事件流当前末尾游标；空流返回0-0。"""

        rows = self.redis_client.xrevrange(
            self.stream_name,
            max="+",
            min="-",
            count=1,
        )
        if not rows:
            return "0-0"
        message_id = rows[0][0]
        return message_id.decode() if isinstance(message_id, bytes) else str(message_id)

    @staticmethod
    def parse_fields(fields: dict[str, str]) -> RealtimeEventEnvelope:
        raw = fields.get("payload", "")
        if not raw:
            raise ValueError("实时事件缺少payload")
        try:
            return RealtimeEventEnvelope.model_validate_json(raw)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("实时事件payload不合法") from exc
