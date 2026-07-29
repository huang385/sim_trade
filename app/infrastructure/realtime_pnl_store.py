import json
from datetime import datetime
from decimal import Decimal
from typing import Iterable

from redis import Redis

from app.infrastructure.redis_keys import (
    PNL_ACCOUNT_INDEX_KEYS_KEY,
    PNL_CONTRACT_INDEX_KEYS_KEY,
    PNL_DIRTY_ACCOUNT_FACTS_KEY,
    PNL_DIRTY_ACCOUNT_FACT_VERSIONS_KEY,
    PNL_DIRTY_CONTRACTS_KEY,
    PNL_DIRTY_CONTRACT_VERSIONS_KEY,
    PNL_DIRTY_ACCOUNTS_KEY,
    PNL_DIRTY_POSITIONS_KEY,
    PNL_DIRTY_POSITION_VERSIONS_KEY,
    PNL_POSITION_CACHE_VERSION_KEY,
    PNL_WORKER_LEASE_KEY,
    parse_pnl_dirty_contract_member,
    pnl_account_key,
    pnl_account_positions_key,
    pnl_contract_positions_key,
    pnl_dirty_contract_accounts_key,
    pnl_dirty_contract_member,
    pnl_position_key,
    processed_pnl_fact_event_key,
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

CLEAR_DIRTY_CONTRACT_IF_UNCHANGED_SCRIPT = """
local current = redis.call('HGET', KEYS[1], ARGV[1])
if current == ARGV[2] then
    redis.call('SREM', KEYS[2], ARGV[1])
    redis.call('HDEL', KEYS[1], ARGV[1])
    redis.call('DEL', KEYS[3])
    return 1
end
return 0
"""

RELEASE_LEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

RENEW_LEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

MARK_CONTRACT_DIRTY_SCRIPT = """
local version = redis.call('INCR', KEYS[1])
redis.call('SADD', KEYS[2], ARGV[1])
redis.call('HSET', KEYS[3], ARGV[1], tostring(version))
redis.call('SADD', KEYS[4], ARGV[2])
return tostring(version)
"""

MARK_CONTRACT_DIRTY_ONCE_SCRIPT = """
if redis.call('EXISTS', KEYS[5]) == 1 then
    return ''
end
local version = redis.call('INCR', KEYS[1])
redis.call('SADD', KEYS[2], ARGV[1])
redis.call('HSET', KEYS[3], ARGV[1], tostring(version))
redis.call('SADD', KEYS[4], ARGV[2])
redis.call('SET', KEYS[5], tostring(version), 'EX', ARGV[3])
return tostring(version)
"""

MARK_ACCOUNT_FACT_DIRTY_ONCE_SCRIPT = """
if redis.call('EXISTS', KEYS[3]) == 1 then
    return ''
end
local version = redis.call('HINCRBY', KEYS[2], ARGV[1], 1)
redis.call('SADD', KEYS[1], ARGV[1])
redis.call('SET', KEYS[3], tostring(version), 'EX', ARGV[2])
return tostring(version)
"""


APPLY_CYCLE_OPERATIONS_BODY = """
local operations = cjson.decode(ARGV[2])
for _, operation in ipairs(operations) do
    local command = operation[1]
    local key = operation[2]
    if command == 'HSET' then
        for index = 3, #operation, 2 do
            redis.call('HSET', key, operation[index], operation[index + 1])
        end
    elseif command == 'SADD' then
        redis.call('SADD', key, operation[3])
    elseif command == 'SREM_MEMBER_AND_PRUNE_INDEX' then
        redis.call('SREM', key, operation[3])
        if redis.call('SCARD', key) == 0 then
            redis.call('SREM', operation[4], key)
        end
    end
end
return 1
"""

WRITE_CYCLE_SCRIPT = (
    "local unused = ARGV[1]\n" + APPLY_CYCLE_OPERATIONS_BODY
)

# Redis 5中租约键自然过期与WATCH的组合不能提供本场景要求的严格屏障，因此
# 最终快照写入使用Lua完成“检查持有者+执行预生成命令”。Lua只搬运Python
# 已经计算好的字符串，不解析、不比较、更不计算任何金额。
WRITE_CYCLE_IF_LEASE_OWNED_SCRIPT = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return 0
end
""" + APPLY_CYCLE_OPERATIONS_BODY


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

    def __init__(
        self,
        redis_client: Redis,
        *,
        worker_lease_key: str = PNL_WORKER_LEASE_KEY,
    ):
        self.redis_client = redis_client
        # 生产环境使用固定全局键；集成测试可注入隔离键，避免干扰正在运行的
        # 实时PnL Worker，同时不改变Lua租约屏障语义。
        self.worker_lease_key = worker_lease_key

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

    def mark_contract_dirty(
        self,
        *,
        exchange_id: str,
        symbol: str,
        account_id: str,
    ) -> str:
        """
        原子递增持仓缓存版本并记录跨进程Dirty合约。

        版本只作为字符串CAS标识，不参与任何资金计算。关联账户集合用于全部
        平仓后补读账户基础字段。
        """

        member = pnl_dirty_contract_member(exchange_id, symbol)
        return str(
            self.redis_client.eval(
                MARK_CONTRACT_DIRTY_SCRIPT,
                4,
                PNL_POSITION_CACHE_VERSION_KEY,
                PNL_DIRTY_CONTRACTS_KEY,
                PNL_DIRTY_CONTRACT_VERSIONS_KEY,
                pnl_dirty_contract_accounts_key(exchange_id, symbol),
                member,
                account_id,
            )
        )

    def mark_contract_dirty_once(
        self,
        *,
        event_id: str,
        exchange_id: str,
        symbol: str,
        account_id: str,
        processed_ttl_seconds: int,
    ) -> str | None:
        """
        以事件编号幂等标记账户/持仓事实变化并递增缓存版本。

        Outbox消息可能在ACK前重投；同一事件只允许触发一次版本变化，避免重复
        投递让实时Worker无意义地反复全量刷新活动持仓缓存。
        """

        member = pnl_dirty_contract_member(exchange_id, symbol)
        version = self.redis_client.eval(
            MARK_CONTRACT_DIRTY_ONCE_SCRIPT,
            5,
            PNL_POSITION_CACHE_VERSION_KEY,
            PNL_DIRTY_CONTRACTS_KEY,
            PNL_DIRTY_CONTRACT_VERSIONS_KEY,
            pnl_dirty_contract_accounts_key(exchange_id, symbol),
            processed_pnl_fact_event_key(event_id),
            member,
            account_id,
            processed_ttl_seconds,
        )
        return str(version) if version not in (None, "") else None

    def mark_account_fact_dirty_once(
        self,
        *,
        event_id: str,
        account_id: str,
        processed_ttl_seconds: int,
    ) -> str | None:
        """
        幂等标记账户基础资金事实发生变化。

        订单接受和撤单只会改变冻结资金、手续费等账户字段，不应递增持仓
        结构版本。每个账户使用独立整数版本，处理期间出现新事件时，旧版本
        的CAS清理不会误删新Dirty。
        """

        version = self.redis_client.eval(
            MARK_ACCOUNT_FACT_DIRTY_ONCE_SCRIPT,
            3,
            PNL_DIRTY_ACCOUNT_FACTS_KEY,
            PNL_DIRTY_ACCOUNT_FACT_VERSIONS_KEY,
            processed_pnl_fact_event_key(event_id),
            account_id,
            processed_ttl_seconds,
        )
        return str(version) if version not in (None, "") else None

    def list_dirty_account_facts(self) -> list[tuple[str, str]]:
        """批量读取需要刷新PostgreSQL账户基础字段的账户及其版本。"""

        account_ids = sorted(
            self.redis_client.smembers(PNL_DIRTY_ACCOUNT_FACTS_KEY)
        )
        if not account_ids:
            return []
        versions = self.redis_client.hmget(
            PNL_DIRTY_ACCOUNT_FACT_VERSIONS_KEY,
            account_ids,
        )
        return [
            (account_id, version or "")
            for account_id, version in zip(
                account_ids,
                versions,
                strict=True,
            )
        ]

    def complete_dirty_account_fact(
        self,
        account_id: str,
        expected_version: str,
    ) -> bool:
        """仅在账户事实版本未变化时清除Dirty，避免覆盖并发新事件。"""

        return bool(
            self.redis_client.eval(
                CLEAR_DIRTY_IF_UNCHANGED_SCRIPT,
                2,
                PNL_DIRTY_ACCOUNT_FACT_VERSIONS_KEY,
                PNL_DIRTY_ACCOUNT_FACTS_KEY,
                account_id,
                expected_version,
            )
        )

    def list_dirty_contracts(
        self,
    ) -> list[tuple[tuple[str, str], str, set[str]]]:
        """读取当前Dirty合约、版本及成交关联账户。"""

        members = sorted(self.redis_client.smembers(PNL_DIRTY_CONTRACTS_KEY))
        if not members:
            return []
        versions = self.redis_client.hmget(
            PNL_DIRTY_CONTRACT_VERSIONS_KEY,
            members,
        )
        pipeline = self.redis_client.pipeline(transaction=False)
        keys: list[tuple[str, str]] = []
        for member in members:
            key = parse_pnl_dirty_contract_member(member)
            keys.append(key)
            pipeline.smembers(
                pnl_dirty_contract_accounts_key(*key)
            )
        accounts = pipeline.execute()
        return [
            (key, str(version or ""), set(account_ids or ()))
            for key, version, account_ids in zip(
                keys,
                versions,
                accounts,
                strict=True,
            )
        ]

    def complete_dirty_contract(
        self,
        *,
        exchange_id: str,
        symbol: str,
        expected_version: str,
    ) -> bool:
        """
        仅清除仍等于本轮读取版本的Dirty标记。

        清理成功后删除本轮成交账户集合；如果计算期间又有成交，版本已变化，
        Lua返回0并保留新Dirty及其账户信息。
        """

        member = pnl_dirty_contract_member(exchange_id, symbol)
        completed = bool(
            self.redis_client.eval(
                CLEAR_DIRTY_CONTRACT_IF_UNCHANGED_SCRIPT,
                3,
                PNL_DIRTY_CONTRACT_VERSIONS_KEY,
                PNL_DIRTY_CONTRACTS_KEY,
                pnl_dirty_contract_accounts_key(exchange_id, symbol),
                member,
                expected_version,
            )
        )
        return completed

    def acquire_worker_lease(self, owner: str, ttl_seconds: int) -> bool:
        """抢占实时PnL快照单写者租约。"""

        return bool(
            self.redis_client.set(
                self.worker_lease_key,
                owner,
                nx=True,
                ex=ttl_seconds,
            )
        )

    def renew_worker_lease(self, owner: str, ttl_seconds: int) -> bool:
        """只有租约持有者可以续租。"""

        return bool(
            self.redis_client.eval(
                RENEW_LEASE_SCRIPT,
                1,
                self.worker_lease_key,
                owner,
                ttl_seconds,
            )
        )

    def release_worker_lease(self, owner: str) -> bool:
        """退出时只删除属于当前实例的租约。"""

        return bool(
            self.redis_client.eval(
                RELEASE_LEASE_SCRIPT,
                1,
                self.worker_lease_key,
                owner,
            )
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

    @staticmethod
    def _build_cycle_operations(
        *,
        positions: list[PositionRealtimePnl],
        accounts: list[AccountRealtimePnl],
        dirty_version: str,
        additions: list[tuple[str, str, str, str]],
        removals: list[tuple[str, str, str, str]],
    ) -> list[list[str]]:
        """
        生成一个PnL周期的Redis绝对写入命令。

        Lua只执行字符串和集合操作。删除最后一条持仓索引时，SCARD检查和
        元索引清理位于同一个原子脚本中，不会误删并发新增的有效索引。
        """

        operations: list[list[str]] = []
        for item in positions:
            hash_operation = ["HSET", pnl_position_key(item.position_id)]
            for field, value in _mapping(item).items():
                hash_operation.extend((field, value))
            operations.append(hash_operation)
            operations.append(
                ["SADD", PNL_DIRTY_POSITIONS_KEY, item.position_id]
            )
            operations.append(
                [
                    "HSET",
                    PNL_DIRTY_POSITION_VERSIONS_KEY,
                    item.position_id,
                    dirty_version,
                ]
            )

        for item in accounts:
            hash_operation = ["HSET", pnl_account_key(item.account_id)]
            for field, value in _mapping(item).items():
                hash_operation.extend((field, value))
            operations.append(hash_operation)
            operations.append(
                ["SADD", PNL_DIRTY_ACCOUNTS_KEY, item.account_id]
            )

        for account_id, exchange_id, symbol, position_id in additions:
            account_key = pnl_account_positions_key(account_id)
            contract_key = pnl_contract_positions_key(exchange_id, symbol)
            operations.extend(
                (
                    ["SADD", account_key, position_id],
                    ["SADD", contract_key, position_id],
                    ["SADD", PNL_ACCOUNT_INDEX_KEYS_KEY, account_key],
                    ["SADD", PNL_CONTRACT_INDEX_KEYS_KEY, contract_key],
                )
            )

        for account_id, exchange_id, symbol, position_id in removals:
            operations.extend(
                (
                    [
                        "SREM_MEMBER_AND_PRUNE_INDEX",
                        pnl_account_positions_key(account_id),
                        position_id,
                        PNL_ACCOUNT_INDEX_KEYS_KEY,
                    ],
                    [
                        "SREM_MEMBER_AND_PRUNE_INDEX",
                        pnl_contract_positions_key(exchange_id, symbol),
                        position_id,
                        PNL_CONTRACT_INDEX_KEYS_KEY,
                    ],
                )
            )
        return operations

    def write_cycle_snapshots(
        self,
        *,
        positions: Iterable[PositionRealtimePnl],
        accounts: Iterable[AccountRealtimePnl],
        dirty_version: str,
        active_positions: Iterable[tuple[str, str, str, str]],
        closed_positions: Iterable[tuple[str, str, str, str]],
    ) -> tuple[int, int]:
        """
        一个500ms批次内原子写快照、Dirty标记和必要的静态索引变化。

        active_positions/closed_positions元素依次为账户、交易所、合约、持仓
        编号。行情价格变化不会重复维护索引，只有调用方确认结构变化或首次
        恢复时才传入。
        """

        position_items = list(positions)
        account_items = list(accounts)
        operations = self._build_cycle_operations(
            positions=position_items,
            accounts=account_items,
            dirty_version=dirty_version,
            additions=list(active_positions),
            removals=list(closed_positions),
        )
        self.redis_client.eval(
            WRITE_CYCLE_SCRIPT,
            0,
            "",
            json.dumps(
                operations,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        return len(position_items), len(account_items)

    def write_cycle_snapshots_if_lease_owned(
        self,
        *,
        lease_owner: str,
        positions: Iterable[PositionRealtimePnl],
        accounts: Iterable[AccountRealtimePnl],
        dirty_version: str,
        active_positions: Iterable[tuple[str, str, str, str]],
        closed_positions: Iterable[tuple[str, str, str, str]],
    ) -> tuple[bool, int, int]:
        """
        仅在当前实例仍持有租约时原子写入一个PnL周期的全部Redis变化。

        返回False表示租约已经过期或被其他Worker取得，此时脚本不会执行任何
        快照、Dirty或索引写入。金额始终作为字符串传入Lua，脚本不做资金计算。
        """

        position_items = list(positions)
        account_items = list(accounts)
        operations = self._build_cycle_operations(
            positions=position_items,
            accounts=account_items,
            dirty_version=dirty_version,
            additions=list(active_positions),
            removals=list(closed_positions),
        )

        written = bool(
            self.redis_client.eval(
                WRITE_CYCLE_IF_LEASE_OWNED_SCRIPT,
                1,
                self.worker_lease_key,
                lease_owner,
                json.dumps(
                    operations,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        )
        if not written:
            return False, 0, 0
        return True, len(position_items), len(account_items)

    def get_positions_many(
        self,
        position_ids: Iterable[str],
    ) -> dict[str, dict[str, str]]:
        """使用一个非事务Pipeline批量读取持仓快照，消除逐持仓往返。"""

        ids = list(dict.fromkeys(position_ids))
        if not ids:
            return {}
        pipeline = self.redis_client.pipeline(transaction=False)
        for position_id in ids:
            pipeline.hgetall(pnl_position_key(position_id))
        return dict(zip(ids, pipeline.execute(), strict=True))

    def get_accounts_many(
        self,
        account_ids: Iterable[str],
    ) -> dict[str, dict[str, str]]:
        """批量读取账户快照。"""

        ids = list(dict.fromkeys(account_ids))
        if not ids:
            return {}
        pipeline = self.redis_client.pipeline(transaction=False)
        for account_id in ids:
            pipeline.hgetall(pnl_account_key(account_id))
        return dict(zip(ids, pipeline.execute(), strict=True))

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

    def list_contract_position_ids_many(
        self,
        contract_keys: Iterable[tuple[str, str]],
    ) -> dict[tuple[str, str], set[str]]:
        """使用一次Pipeline批量读取多个合约的实时持仓索引。"""

        keys = sorted(
            {
                (
                    str(exchange_id).strip().upper(),
                    str(symbol).strip().upper(),
                )
                for exchange_id, symbol in contract_keys
            }
        )
        if not keys:
            return {}
        pipeline = self.redis_client.pipeline(transaction=False)
        for exchange_id, symbol in keys:
            pipeline.smembers(
                pnl_contract_positions_key(exchange_id, symbol)
            )
        values = pipeline.execute()
        return {
            key: set(position_ids or ())
            for key, position_ids in zip(keys, values, strict=True)
        }

    def list_active_contract_codes(self) -> set[str]:
        """
        返回当前至少包含一条有效持仓的合约代码集合。

        PNL_CONTRACT_INDEX_KEYS_KEY只保存持仓合约索引的键名，具体合约集合
        可能在全部平仓后暂时变为空集合。因此先一次读取索引键名，再通过
        Pipeline批量检查SCARD，只把仍有持仓编号的合约交给行情订阅服务。
        这里不在读取路径删除空索引，避免与并发新建持仓发生竞态；空索引由
        现有周期性重建统一清理。整个过程不会高频查询PostgreSQL。
        """

        raw_keys = self.redis_client.smembers(
            PNL_CONTRACT_INDEX_KEYS_KEY
        )
        index_keys = sorted(
            value.decode("utf-8")
            if isinstance(value, bytes)
            else str(value)
            for value in raw_keys
        )
        if not index_keys:
            return set()

        pipeline = self.redis_client.pipeline(transaction=False)
        for index_key in index_keys:
            pipeline.scard(index_key)
        member_counts = pipeline.execute()

        prefix = "pnl:contract_positions:"
        codes: set[str] = set()
        for index_key, member_count in zip(
            index_keys,
            member_counts,
            strict=True,
        ):
            if int(member_count or 0) <= 0:
                continue
            if not index_key.startswith(prefix):
                continue
            contract_part = index_key[len(prefix):]
            _exchange_id, separator, symbol = contract_part.partition(":")
            normalized_symbol = symbol.strip().upper()
            if separator and normalized_symbol:
                codes.add(normalized_symbol)
        return codes

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
        # SSCAN按游标有界读取，避免Dirty集合很大时SMEMBERS一次搬入全部成员。
        position_ids: list[str] = []
        cursor = 0
        while len(position_ids) < batch_size:
            cursor, members = self.redis_client.sscan(
                PNL_DIRTY_POSITIONS_KEY,
                cursor=cursor,
                count=batch_size,
            )
            position_ids.extend(members)
            if cursor == 0:
                break
        position_ids = list(dict.fromkeys(position_ids))[:batch_size]
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

    def rebuild_active_indexes(
        self,
        *,
        expected_cache_version: str,
        positions: Iterable[tuple[str, str, str, str]],
    ) -> bool:
        """
        按PostgreSQL周期快照重建PnL活动索引，不使用KEYS扫描。

        WATCH确保数据库读取期间若成交递增了缓存版本，本次不会清空新索引，
        Worker会在下一周期使用新版本重试。
        """

        items = list(positions)
        for _attempt in range(3):
            pipeline = self.redis_client.pipeline()
            try:
                pipeline.watch(PNL_POSITION_CACHE_VERSION_KEY)
                current = str(
                    pipeline.get(PNL_POSITION_CACHE_VERSION_KEY) or "0"
                )
                if current != str(expected_cache_version or "0"):
                    pipeline.unwatch()
                    return False
                account_keys = set(
                    pipeline.smembers(PNL_ACCOUNT_INDEX_KEYS_KEY)
                )
                contract_keys = set(
                    pipeline.smembers(PNL_CONTRACT_INDEX_KEYS_KEY)
                )
                pipeline.multi()
                stale_keys = sorted(account_keys | contract_keys)
                if stale_keys:
                    pipeline.delete(*stale_keys)
                pipeline.delete(
                    PNL_ACCOUNT_INDEX_KEYS_KEY,
                    PNL_CONTRACT_INDEX_KEYS_KEY,
                )
                for account_id, exchange_id, symbol, position_id in items:
                    account_key = pnl_account_positions_key(account_id)
                    contract_key = pnl_contract_positions_key(
                        exchange_id,
                        symbol,
                    )
                    pipeline.sadd(account_key, position_id)
                    pipeline.sadd(contract_key, position_id)
                    pipeline.sadd(
                        PNL_ACCOUNT_INDEX_KEYS_KEY,
                        account_key,
                    )
                    pipeline.sadd(
                        PNL_CONTRACT_INDEX_KEYS_KEY,
                        contract_key,
                    )
                pipeline.execute()
                return True
            except WatchError:
                continue
            finally:
                pipeline.reset()
        return False
