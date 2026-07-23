import redis
from app.core.config import settings


redis_client = redis.Redis.from_url(
    settings.redis_url,
    decode_responses=True,
    protocol=2,
    # redis-py 8 在未显式配置时会在约5秒触发读取边界，而订单Consumer
    # 默认执行 BLOCK 5000。增加至少1秒缓冲，正常无消息时由Redis返回空列表，
    # 真正超过套接字超时的连接故障仍会抛出异常并由Worker重试。
    socket_timeout=max(
        settings.redis_socket_timeout_seconds,
        settings.order_consumer_block_ms / 1000 + 1,
        settings.market_matching_block_ms / 1000 + 1,
    ),
)


def check_redis() -> bool:
    return redis_client.ping()
