from uuid import uuid4

import pytest
from redis.exceptions import RedisError

from app.core.redis_client import redis_client
import app.infrastructure.realtime_pnl_store as pnl_store_module
from app.infrastructure.realtime_pnl_store import RealtimePnlStore


def test_more_than_500_dirty_positions_rotate_without_starvation(
    monkeypatch,
):
    """
    首批成员即使始终留在Dirty Set，保存的SSCAN游标也必须让后续成员被看到。

    使用测试专属Redis键，不清空业务库，也不会与正在运行的PnL Worker争抢。
    每轮重新创建Store实例，用于验证游标确实跨进程保存在Redis中。
    """

    try:
        redis_client.ping()
    except RedisError as exc:
        pytest.skip(f"Redis不可连接: {exc}")

    suffix = uuid4().hex
    dirty_key = f"test:pnl:dirty_positions:{suffix}"
    version_key = f"test:pnl:dirty_position_versions:{suffix}"
    cursor_key = f"test:pnl:dirty_position_scan_cursor:{suffix}"
    buffer_key = f"test:pnl:dirty_position_scan_buffer:{suffix}"
    monkeypatch.setattr(
        pnl_store_module,
        "PNL_DIRTY_POSITIONS_KEY",
        dirty_key,
    )
    monkeypatch.setattr(
        pnl_store_module,
        "PNL_DIRTY_POSITION_VERSIONS_KEY",
        version_key,
    )
    monkeypatch.setattr(
        pnl_store_module,
        "PNL_DIRTY_POSITION_SCAN_CURSOR_KEY",
        cursor_key,
    )
    monkeypatch.setattr(
        pnl_store_module,
        "PNL_DIRTY_POSITION_SCAN_BUFFER_KEY",
        buffer_key,
    )

    position_ids = {f"P-{index:04d}" for index in range(650)}
    try:
        redis_client.sadd(dirty_key, *position_ids)
        redis_client.hset(
            version_key,
            mapping={
                position_id: f"v-{position_id}"
                for position_id in position_ids
            },
        )

        seen: set[str] = set()
        for _ in range(20):
            # 模拟Worker重启：每一轮都使用全新的Store对象。
            batch = RealtimePnlStore(
                redis_client
            ).list_dirty_positions(100)
            seen.update(position_id for position_id, _ in batch)
            if seen == position_ids:
                break

        assert seen == position_ids
        assert redis_client.scard(dirty_key) == 650
    finally:
        redis_client.delete(
            dirty_key,
            version_key,
            cursor_key,
            buffer_key,
        )
