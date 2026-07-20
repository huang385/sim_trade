from app.core.config import settings
from app.core.redis_client import redis_client


def test_socket_timeout_is_longer_than_stream_block_time():
    """阻塞读取的正常空闲不能先触发Redis客户端套接字超时。"""

    socket_timeout = redis_client.connection_pool.connection_kwargs[
        "socket_timeout"
    ]
    assert socket_timeout > settings.order_consumer_block_ms / 1000
