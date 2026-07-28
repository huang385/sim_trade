from datetime import datetime
from decimal import Decimal
from typing import Iterable

from redis import Redis

from app.infrastructure.redis_keys import (
    PNL_DIRTY_ACCOUNTS_KEY,
    PNL_DIRTY_POSITIONS_KEY,
    PNL_DIRTY_POSITION_VERSIONS_KEY,
    PNL_POSITION_CACHE_VERSION_KEY,
    pnl_account_key,
    pnl_account_positions_key,
    pnl_contract_positions_key,
    pnl_position_key,
)
from app.schemas.pnl_schema import (
    AccountRealtimePnl,
    PositionRealtimePnl,
)


CLEAR_DIRTY_IF_UNCHANGED_SCRIPT = """
local current = redis.call('HGET', KEYS[1], ARGV[1])
if current == ARGV[2] then
    redis.call('SREM', KEYS[2], ARGV[1])
    redis.call('HDEL', KEYS[1], ARGV[1])
    return 1
end
return 0
"""


def _redis_value(value) -> str:
    """金额始终保存为十进制字符串，禁止float和HINCRBYFLOAT。"""

    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _mapping(model) -> dict[str, str]:
    return {
        key: _redis_value(value)
        for key, value in model.model_dump(mode="python").items()
    }


class RealtimePnlStore:
    """只负责Redis实时盈亏Hash、索引和可靠Dirty标记读写。"""

    def __init__(self, redis_client: Redis):
        self.redis_client = redis_client

    def bump_position_cache_version(self) -> str:
        """成交提交后递增跨进程缓存版本；该整数仅用于失效，不参与金额计算。"""

        return str(
            self.redis_client.incr(PNL_POSITION_CACHE_VERSION_KEY)
        )

    def get_position_cache_version(self) -> str:
        """读取跨进程活动持仓缓存版本，键未建立时使用初始版本0。"""

        return str(
            self.redis_client.get(PNL_POSITION_CACHE_VERSION_KEY) or "0"
        )

    def write_snapshots(
        self,
        *,
        positions: Iterable[PositionRealtimePnl],
        accounts: Iterable[AccountRealtimePnl],
        dirty_version: str,
    ) -> tuple[int, int]:
        """事务Pipeline原子写入Python已计算好的绝对Decimal字符串。"""

        position_items = list(positions)
        account_items = list(accounts)
        pipeline = self.redis_client.pipeline(transaction=True)
        for item in position_items:
            pipeline.hset(
                pnl_position_key(item.position_id),
                mapping=_mapping(item),
            )
            pipeline.sadd(
                pnl_account_positions_key(item.account_id),
                item.position_id,
            )
            pipeline.sadd(
                pnl_contract_positions_key(
                    item.exchange_id,
                    item.symbol,
                ),
                item.position_id,
            )
            pipeline.sadd(PNL_DIRTY_POSITIONS_KEY, item.position_id)
            pipeline.hset(
                PNL_DIRTY_POSITION_VERSIONS_KEY,
                item.position_id,
                dirty_version,
            )
        for item in account_items:
            pipeline.hset(
                pnl_account_key(item.account_id),
                mapping=_mapping(item),
            )
            pipeline.sadd(PNL_DIRTY_ACCOUNTS_KEY, item.account_id)
        pipeline.execute()
        return len(position_items), len(account_items)

    def get_position(
        self,
        position_id: str,
    ) -> dict[str, str]:
        return self.redis_client.hgetall(
            pnl_position_key(position_id)
        )

    def get_account(self, account_id: str) -> dict[str, str]:
        return self.redis_client.hgetall(pnl_account_key(account_id))

    def list_contract_position_ids(
        self,
        exchange_id: str,
        symbol: str,
    ) -> set[str]:
        return set(
            self.redis_client.smembers(
                pnl_contract_positions_key(exchange_id, symbol)
            )
        )

    def remove_contract_position(
        self,
        *,
        exchange_id: str,
        symbol: str,
        account_id: str,
        position_id: str,
    ) -> None:
        pipeline = self.redis_client.pipeline(transaction=True)
        pipeline.srem(
            pnl_contract_positions_key(exchange_id, symbol),
            position_id,
        )
        pipeline.srem(
            pnl_account_positions_key(account_id),
            position_id,
        )
        pipeline.execute()

    def list_dirty_positions(
        self,
        batch_size: int,
    ) -> list[tuple[str, str]]:
        position_ids = sorted(
            self.redis_client.smembers(PNL_DIRTY_POSITIONS_KEY)
        )[:batch_size]
        if not position_ids:
            return []
        versions = self.redis_client.hmget(
            PNL_DIRTY_POSITION_VERSIONS_KEY,
            position_ids,
        )
        return [
            (position_id, version or "")
            for position_id, version in zip(
                position_ids,
                versions,
                strict=True,
            )
        ]

    def complete_dirty_position(
        self,
        position_id: str,
        expected_version: str,
    ) -> bool:
        """仅当期间没有新Tick覆盖版本时删除Dirty，避免丢失并发更新。"""

        return bool(
            self.redis_client.eval(
                CLEAR_DIRTY_IF_UNCHANGED_SCRIPT,
                2,
                PNL_DIRTY_POSITION_VERSIONS_KEY,
                PNL_DIRTY_POSITIONS_KEY,
                position_id,
                expected_version,
            )
        )

    def complete_dirty_account(self, account_id: str) -> None:
        """账户落库后清理辅助Dirty标记；持仓版本仍是可靠性主标记。"""

        self.redis_client.srem(PNL_DIRTY_ACCOUNTS_KEY, account_id)
