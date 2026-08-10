import json
from datetime import datetime
from decimal import Decimal
from typing import Iterable

from redis import Redis
from redis.exceptions import WatchError

from app.core.config import settings

from app.infrastructure.redis_keys import (
    PNL_ACCOUNT_INDEX_KEYS_KEY,
    PNL_ACCOUNT_REALTIME_VERSIONS_KEY,
    PNL_CONTRACT_INDEX_KEYS_KEY,
    PNL_CONTRACT_ORDER_BOOK_IDS_KEY,
    PNL_DIRTY_ACCOUNT_FACTS_KEY,
    PNL_DIRTY_ACCOUNT_FACT_VERSIONS_KEY,
    PNL_DIRTY_CONTRACTS_KEY,
    PNL_DIRTY_CONTRACT_VERSIONS_KEY,
    PNL_DIRTY_ACCOUNTS_KEY,
    PNL_DIRTY_ACCOUNT_SCAN_BUFFER_KEY,
    PNL_DIRTY_ACCOUNT_SCAN_CURSOR_KEY,
    PNL_DIRTY_ACCOUNT_VERSIONS_KEY,
    PNL_DIRTY_POSITIONS_KEY,
    PNL_DIRTY_POSITION_SCAN_BUFFER_KEY,
    PNL_DIRTY_POSITION_SCAN_CURSOR_KEY,
    PNL_DIRTY_POSITION_VERSIONS_KEY,
    PNL_POSITION_CACHE_VERSION_KEY,
    PNL_POSITION_REALTIME_VERSIONS_KEY,
    PNL_REALTIME_SNAPSHOT_SEQUENCE_KEY,
    PNL_WORKER_LEASE_KEY,
    REALTIME_EVENT_STREAM,
    parse_pnl_dirty_contract_member,
    pnl_account_key,
    pnl_account_positions_key,
    pnl_contract_positions_key,
    pnl_dirty_account_contracts_key,
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

CLEAR_DIRTY_IF_VERSION_MISSING_SCRIPT = """
if redis.call('HEXISTS', KEYS[1], ARGV[1]) == 0 then
    redis.call('SREM', KEYS[2], ARGV[1])
    return 1
end
return 0
"""

# 账户事实版本是跨处理周期的永久单调计数器。完成当前版本时只移除Dirty
# 集合成员，不能HDEL版本字段，否则下一次HINCRBY会从1重新开始。
CLEAR_ACCOUNT_FACT_DIRTY_IF_UNCHANGED_SCRIPT = """
local current = redis.call('HGET', KEYS[1], ARGV[1])
if current == ARGV[2] then
    redis.call('SREM', KEYS[2], ARGV[1])
    return 1
end
return 0
"""

POP_DIRTY_SCAN_BUFFER_SCRIPT = """
local items = redis.call('LRANGE', KEYS[1], 0, tonumber(ARGV[1]) - 1)
redis.call('LTRIM', KEYS[1], tonumber(ARGV[1]), -1)
return items
"""

CLEAR_DIRTY_CONTRACT_IF_UNCHANGED_SCRIPT = """
local current = redis.call('HGET', KEYS[1], ARGV[1])
if current == ARGV[2] then
    local accounts = redis.call('SMEMBERS', KEYS[3])
    redis.call('SREM', KEYS[2], ARGV[1])
    redis.call('HDEL', KEYS[1], ARGV[1])
    for _, account_id in ipairs(accounts) do
        local account_key = ARGV[3] .. account_id
        redis.call('SREM', account_key, ARGV[1])
        if redis.call('SCARD', account_key) == 0 then
            redis.call('DEL', account_key)
        end
    end
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
redis.call('SADD', KEYS[5], ARGV[1])
return tostring(version)
"""

MARK_CONTRACT_DIRTY_ONCE_SCRIPT = """
if redis.call('EXISTS', KEYS[6]) == 1 then
    return ''
end
local version = redis.call('INCR', KEYS[1])
redis.call('SADD', KEYS[2], ARGV[1])
redis.call('HSET', KEYS[3], ARGV[1], tostring(version))
redis.call('SADD', KEYS[4], ARGV[2])
redis.call('SADD', KEYS[5], ARGV[1])
redis.call('SET', KEYS[6], tostring(version), 'EX', ARGV[3])
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
    elseif command == 'HSET_REALTIME_SNAPSHOT' then
        -- operation[3]是实体版本Hash，operation[4]是账户或持仓编号。
        -- 金额仍由Python预先计算；Lua只附加周期版本并执行Redis命令。
        for index = 5, #operation, 2 do
            redis.call('HSET', key, operation[index], operation[index + 1])
        end
        redis.call('HSET', key, 'realtime_snapshot_version', snapshot_version)
        redis.call('HSET', operation[3], operation[4], snapshot_version)
    elseif command == 'SADD' then
        redis.call('SADD', key, operation[3])
    elseif command == 'SREM_MEMBER_AND_PRUNE_INDEX' then
        redis.call('SREM', key, operation[3])
        if redis.call('SCARD', key) == 0 then
            redis.call('SREM', operation[4], key)
        end
    elseif command == 'XADD_REALTIME_EVENT' then
        local envelope = cjson.decode(operation[7])
        envelope['realtime_version'] = snapshot_version
        if envelope['payload'] then
            envelope['payload']['realtime_snapshot_version'] = snapshot_version
        end
        redis.call(
            'XADD', key, 'MAXLEN', '~', operation[8], '*',
            'event_id', operation[3],
            'event_type', operation[4],
            'account_id', operation[5],
            'entity_id', operation[6],
            'payload', cjson.encode(envelope)
        )
    end
end
return snapshot_version
"""

WRITE_CYCLE_SCRIPT = """
local current_cache_version = tostring(
    redis.call('GET', KEYS[2]) or '0'
)
if ARGV[1] ~= '' and current_cache_version ~= ARGV[1] then
    return 0
end
local snapshot_version = tostring(redis.call('INCR', KEYS[1]))
""" + APPLY_CYCLE_OPERATIONS_BODY

# Redis 5中租约键自然过期与WATCH的组合不能提供本场景要求的严格屏障，因此
# 最终快照写入使用Lua完成“检查持有者+执行预生成命令”。Lua只搬运Python
# 已经计算好的字符串，不解析、不比较、更不计算任何金额。
WRITE_CYCLE_IF_LEASE_OWNED_SCRIPT = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return 0
end
local current_cache_version = tostring(
    redis.call('GET', KEYS[3]) or '0'
)
if ARGV[2] ~= '' and current_cache_version ~= ARGV[2] then
    return 0
end
local snapshot_version = tostring(redis.call('INCR', KEYS[2]))
""" + APPLY_CYCLE_OPERATIONS_BODY.replace("ARGV[2]", "ARGV[3]")


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
        if value is not None
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
                5,
                PNL_POSITION_CACHE_VERSION_KEY,
                PNL_DIRTY_CONTRACTS_KEY,
                PNL_DIRTY_CONTRACT_VERSIONS_KEY,
                pnl_dirty_contract_accounts_key(exchange_id, symbol),
                pnl_dirty_account_contracts_key(account_id),
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
            6,
            PNL_POSITION_CACHE_VERSION_KEY,
            PNL_DIRTY_CONTRACTS_KEY,
            PNL_DIRTY_CONTRACT_VERSIONS_KEY,
            pnl_dirty_contract_accounts_key(exchange_id, symbol),
            pnl_dirty_account_contracts_key(account_id),
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
        """
        仅在账户事实版本未变化时移除Dirty集合成员。

        版本Hash字段必须永久保留，保证下次账户事实事件继续递增；处理期间若
        产生新版本，字符串CAS不相等，本次完成不会清除新Dirty。
        """

        return bool(
            self.redis_client.eval(
                CLEAR_ACCOUNT_FACT_DIRTY_IF_UNCHANGED_SCRIPT,
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
                "pnl:dirty_account_contracts:",
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
            pipeline.hset(
                PNL_DIRTY_ACCOUNT_VERSIONS_KEY,
                item.account_id,
                dirty_version,
            )
        pipeline.execute()
        return len(position_items), len(account_items)

    @staticmethod
    def _build_cycle_operations(
        *,
        positions: list[PositionRealtimePnl],
        accounts: list[AccountRealtimePnl],
        dirty_version: str,
        additions: list[tuple[str, str, str, str, str]],
        removals: list[tuple[str, str, str, str, str]],
        mark_dirty: bool = True,
    ) -> list[list[str]]:
        """
        生成一个PnL周期的Redis绝对写入命令。

        Lua只执行字符串和集合操作。删除最后一条持仓索引时，SCARD检查和
        元索引清理位于同一个原子脚本中，不会误删并发新增的有效索引。
        """

        operations: list[list[str]] = []
        for item in positions:
            hash_operation = [
                "HSET_REALTIME_SNAPSHOT",
                pnl_position_key(item.position_id),
                PNL_POSITION_REALTIME_VERSIONS_KEY,
                item.position_id,
            ]
            for field, value in _mapping(item).items():
                hash_operation.extend((field, value))
            operations.append(hash_operation)
            if mark_dirty:
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
            event_type = (
                "OPTION_VALUATION_UPDATED"
                if item.instrument_type in {"FUTURES_OPTION", "INDEX_OPTION"}
                else "PNL_UPDATED"
            )
            event_id = (
                f"PNL:{dirty_version}:{item.position_id}:"
                f"{item.updated_at.isoformat()}"
            )
            operations.append(
                [
                    "XADD_REALTIME_EVENT",
                    REALTIME_EVENT_STREAM,
                    event_id,
                    event_type,
                    item.account_id,
                    item.position_id,
                    json.dumps(
                        {
                            "event_id": event_id,
                            "event_type": event_type,
                            "account_id": item.account_id,
                            "entity_id": item.position_id,
                            "occurred_at": item.updated_at.isoformat(),
                            "version": dirty_version,
                            "payload": _mapping(item),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    str(settings.realtime_event_stream_maxlen),
                ]
            )

        for item in accounts:
            hash_operation = [
                "HSET_REALTIME_SNAPSHOT",
                pnl_account_key(item.account_id),
                PNL_ACCOUNT_REALTIME_VERSIONS_KEY,
                item.account_id,
            ]
            for field, value in _mapping(item).items():
                hash_operation.extend((field, value))
            operations.append(hash_operation)
            if mark_dirty:
                operations.append(
                    ["SADD", PNL_DIRTY_ACCOUNTS_KEY, item.account_id]
                )
                operations.append(
                    [
                        "HSET",
                        PNL_DIRTY_ACCOUNT_VERSIONS_KEY,
                        item.account_id,
                        dirty_version,
                    ]
                )
            account_event_id = (
                f"ACCOUNT:{dirty_version}:{item.account_id}:"
                f"{item.updated_at.isoformat()}"
            )
            account_payload = _mapping(item)
            # 账户PnL事件只拥有实时派生金额；风险状态、风险率和风险可用
            # 资金由同周期的RISK_STATE_CHANGED独占，数据库基础事实则由
            # ACCOUNT_FACT_UPDATED负责。Hash仍保存完整估值供严格快照读取。
            pnl_event_payload = {
                field: account_payload[field]
                for field in (
                    "account_id",
                    "cumulative_unrealized_pnl",
                    "daily_position_pnl",
                    "daily_pnl",
                    "equity",
                    "available_cash",
                    "futures_unrealized_pnl",
                    "option_realtime_required_margin",
                    "long_option_market_value",
                    "short_option_market_value",
                    "net_option_market_value",
                    "updated_at",
                    "data_source",
                    "source_account_fact_version",
                )
                if field in account_payload
            }
            operations.append(
                [
                    "XADD_REALTIME_EVENT",
                    REALTIME_EVENT_STREAM,
                    account_event_id,
                    "ACCOUNT_PNL_UPDATED",
                    item.account_id,
                    item.account_id,
                    json.dumps(
                        {
                            "event_id": account_event_id,
                            "event_type": "ACCOUNT_PNL_UPDATED",
                            "account_id": item.account_id,
                            "entity_id": item.account_id,
                            "occurred_at": item.updated_at.isoformat(),
                            "version": dirty_version,
                            "payload": pnl_event_payload,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    str(settings.realtime_event_stream_maxlen),
                ]
            )
            risk_event_id = (
                f"RISK:{dirty_version}:{item.account_id}:"
                f"{item.updated_at.isoformat()}"
            )
            operations.append(
                [
                    "XADD_REALTIME_EVENT",
                    REALTIME_EVENT_STREAM,
                    risk_event_id,
                    "RISK_STATE_CHANGED",
                    item.account_id,
                    item.account_id,
                    json.dumps(
                        {
                            "event_id": risk_event_id,
                            "event_type": "RISK_STATE_CHANGED",
                            "account_id": item.account_id,
                            "entity_id": item.account_id,
                            "occurred_at": item.updated_at.isoformat(),
                            "version": dirty_version,
                            "payload": {
                                "risk_state": item.risk_state,
                                "risk_ratio": account_payload["risk_ratio"],
                                "risk_available_cash": account_payload[
                                    "risk_available_cash"
                                ],
                                "updated_at": account_payload["updated_at"],
                            },
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    str(settings.realtime_event_stream_maxlen),
                ]
            )

        for account_id, exchange_id, symbol, order_book_id, position_id in additions:
            account_key = pnl_account_positions_key(account_id)
            contract_key = pnl_contract_positions_key(exchange_id, symbol)
            operations.extend(
                (
                    ["SADD", account_key, position_id],
                    ["SADD", contract_key, position_id],
                    ["SADD", PNL_ACCOUNT_INDEX_KEYS_KEY, account_key],
                    ["SADD", PNL_CONTRACT_INDEX_KEYS_KEY, contract_key],
                    [
                        "HSET",
                        PNL_CONTRACT_ORDER_BOOK_IDS_KEY,
                        contract_key,
                        order_book_id,
                    ],
                )
            )

        for account_id, exchange_id, symbol, _order_book_id, position_id in removals:
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
        active_positions: Iterable[tuple[str, str, str, str, str]],
        closed_positions: Iterable[tuple[str, str, str, str, str]],
        expected_cache_version: str | None = None,
        mark_dirty: bool = True,
    ) -> tuple[int, int]:
        """
        一个500ms批次内原子写快照、Dirty标记和必要的静态索引变化。

        active_positions/closed_positions元素依次为账户、交易所、内部symbol、
        order_book_id、持仓编号。行情价格变化不会重复维护索引，只有调用方确认结构变化或首次
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
            mark_dirty=mark_dirty,
        )
        written = self.redis_client.eval(
            WRITE_CYCLE_SCRIPT,
            2,
            PNL_REALTIME_SNAPSHOT_SEQUENCE_KEY,
            PNL_POSITION_CACHE_VERSION_KEY,
            str(expected_cache_version or ""),
            json.dumps(
                operations,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        if not written:
            raise RuntimeError(
                "持仓事实版本已变化，拒绝写入旧交易日实时PnL快照"
            )
        return len(position_items), len(account_items)

    def write_cycle_snapshots_if_lease_owned(
        self,
        *,
        lease_owner: str,
        positions: Iterable[PositionRealtimePnl],
        accounts: Iterable[AccountRealtimePnl],
        dirty_version: str,
        active_positions: Iterable[tuple[str, str, str, str, str]],
        closed_positions: Iterable[tuple[str, str, str, str, str]],
        expected_cache_version: str | None = None,
        mark_dirty: bool = True,
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
            mark_dirty=mark_dirty,
        )

        written = bool(
            self.redis_client.eval(
                WRITE_CYCLE_IF_LEASE_OWNED_SCRIPT,
                3,
                self.worker_lease_key,
                PNL_REALTIME_SNAPSHOT_SEQUENCE_KEY,
                PNL_POSITION_CACHE_VERSION_KEY,
                lease_owner,
                str(expected_cache_version or ""),
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

    def get_accounts_with_positions(
        self,
        *,
        account_ids: Iterable[str],
        position_ids: Iterable[str],
    ) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
        """一个Pipeline批量读取多账户及其全部持仓实时快照。"""

        accounts = list(dict.fromkeys(account_ids))
        positions = list(dict.fromkeys(position_ids))
        pipeline = self.redis_client.pipeline(transaction=False)
        for account_id in accounts:
            pipeline.hgetall(pnl_account_key(account_id))
        for position_id in positions:
            pipeline.hgetall(pnl_position_key(position_id))
        rows = pipeline.execute()
        account_rows = rows[: len(accounts)]
        position_rows = rows[len(accounts) :]
        return (
            dict(zip(accounts, account_rows, strict=True)),
            dict(zip(positions, position_rows, strict=True)),
        )

    def get_accounts_with_positions_and_versions(
        self,
        *,
        account_ids: Iterable[str],
        position_ids: Iterable[str],
    ) -> tuple[
        dict[str, dict[str, str]],
        dict[str, dict[str, str]],
        dict[str, str],
        dict[str, str],
        set[str],
        set[str],
    ]:
        """一个Pipeline读取实时Hash、周期版本和账户相关Dirty状态。

        Pipeline内仍是批量命令，不因账户或持仓数量增加网络往返。严格
        WebSocket快照除交叉校验Hash版本外，还要求账户资金事实和持仓结构
        均无未完成Dirty。账户级结构集合也能覆盖最后一条持仓已经关闭、
        当前活动持仓查询已看不到原合约的窗口。
        """

        accounts = list(dict.fromkeys(account_ids))
        positions = list(dict.fromkeys(position_ids))
        pipeline = self.redis_client.pipeline(transaction=False)
        for account_id in accounts:
            pipeline.hgetall(pnl_account_key(account_id))
        for position_id in positions:
            pipeline.hgetall(pnl_position_key(position_id))
        pipeline.hmget(PNL_ACCOUNT_REALTIME_VERSIONS_KEY, accounts)
        pipeline.hmget(PNL_POSITION_REALTIME_VERSIONS_KEY, positions)
        for account_id in accounts:
            pipeline.sismember(PNL_DIRTY_ACCOUNT_FACTS_KEY, account_id)
        for account_id in accounts:
            pipeline.scard(pnl_dirty_account_contracts_key(account_id))
        rows = pipeline.execute()
        account_end = len(accounts)
        position_end = account_end + len(positions)
        account_rows = rows[:account_end]
        position_rows = rows[account_end:position_end]
        account_versions = rows[position_end] if len(rows) > position_end else []
        position_versions = (
            rows[position_end + 1]
            if len(rows) > position_end + 1
            else []
        )
        dirty_fact_start = position_end + 2
        dirty_structure_start = dirty_fact_start + len(accounts)
        dirty_fact_rows = rows[
            dirty_fact_start:dirty_structure_start
        ]
        dirty_structure_rows = rows[
            dirty_structure_start:dirty_structure_start + len(accounts)
        ]
        return (
            dict(zip(accounts, account_rows, strict=True)),
            dict(zip(positions, position_rows, strict=True)),
            {
                account_id: str(version or "")
                for account_id, version in zip(
                    accounts,
                    account_versions,
                    strict=True,
                )
            },
            {
                position_id: str(version or "")
                for position_id, version in zip(
                    positions,
                    position_versions,
                    strict=True,
                )
            },
            {
                account_id
                for account_id, dirty in zip(
                    accounts,
                    dirty_fact_rows,
                    strict=True,
                )
                if dirty
            },
            {
                account_id
                for account_id, count in zip(
                    accounts,
                    dirty_structure_rows,
                    strict=True,
                )
                if int(count or 0) > 0
            },
        )

    def get_account_with_positions(
        self,
        *,
        account_id: str,
        position_ids: Iterable[str],
    ) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
        """
        使用一个Pipeline读取账户快照及其全部持仓快照。

        该方法供账户级交易快照接口使用，避免页面先查账户、再按持仓逐个访问
        Redis。Pipeline只减少网络往返，不改变各Hash独立存储的现有结构。
        """

        ids = list(dict.fromkeys(position_ids))
        pipeline = self.redis_client.pipeline(transaction=False)
        pipeline.hgetall(pnl_account_key(account_id))
        for position_id in ids:
            pipeline.hgetall(pnl_position_key(position_id))
        values = pipeline.execute()
        account_values = values[0] if values else {}
        position_values = dict(
            zip(ids, values[1:], strict=True)
        )
        return account_values, position_values

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
            pipeline.hget(PNL_CONTRACT_ORDER_BOOK_IDS_KEY, index_key)
        rows = pipeline.execute()

        codes: set[str] = set()
        for offset, index_key in enumerate(index_keys):
            member_count = rows[offset * 2]
            order_book_id = rows[offset * 2 + 1]
            if int(member_count or 0) <= 0:
                continue
            normalized_order_book_id = str(order_book_id or "").strip().upper()
            if normalized_order_book_id:
                codes.add(normalized_order_book_id)
        return codes

    def list_margin_dependency_codes(self) -> set[str]:
        """批量返回活动期权空头持仓依赖的标的订阅代码。"""

        index_keys = sorted(
            str(value)
            for value in self.redis_client.smembers(
                PNL_CONTRACT_INDEX_KEYS_KEY
            )
        )
        if not index_keys:
            return set()
        pipeline = self.redis_client.pipeline(transaction=False)
        for index_key in index_keys:
            pipeline.smembers(index_key)
        position_ids = sorted(
            {
                str(position_id)
                for members in pipeline.execute()
                for position_id in (members or ())
            }
        )
        if not position_ids:
            return set()
        pipeline = self.redis_client.pipeline(transaction=False)
        for position_id in position_ids:
            pipeline.hmget(
                pnl_position_key(position_id),
                (
                    "instrument_type",
                    "direction",
                    "underlying_order_book_id",
                ),
            )
        codes: set[str] = set()
        for values in pipeline.execute():
            instrument_type, direction, underlying_code = (
                values or (None, None, None)
            )
            if (
                str(instrument_type or "")
                in {"FUTURES_OPTION", "INDEX_OPTION"}
                and str(direction or "") == "SHORT"
                and underlying_code
            ):
                codes.add(str(underlying_code))
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
        """
        按持久化游标轮转读取Dirty持仓。

        游标保存在Redis中，因此本批无法处理的成员仍留在Set里，但下一批会从
        后续桶继续扫描；即使Worker重启，也不会总从cursor=0读取同一批坏数据。
        """

        if batch_size <= 0:
            return []

        # 上一轮SSCAN可能返回超过batch_size的成员，先原子取出溢出部分。
        # 即使取出后进程崩溃，这些成员仍保留在主Dirty Set，后续扫描可恢复。
        buffered = self.redis_client.eval(
            POP_DIRTY_SCAN_BUFFER_SCRIPT,
            1,
            PNL_DIRTY_POSITION_SCAN_BUFFER_KEY,
            batch_size,
        ) or []
        position_ids: list[str] = list(
            dict.fromkeys(buffered)
        )
        seen = set(position_ids)
        raw_cursor = self.redis_client.get(
            PNL_DIRTY_POSITION_SCAN_CURSOR_KEY
        )
        try:
            cursor = int(raw_cursor or 0)
        except (TypeError, ValueError):
            # 游标键异常时只需从头恢复；Dirty成员本身仍保留在Set中。
            cursor = 0

        start_cursor = cursor
        first_scan = True
        while len(position_ids) < batch_size:
            cursor, members = self.redis_client.sscan(
                PNL_DIRTY_POSITIONS_KEY,
                cursor=cursor,
                count=batch_size,
            )
            overflow: list[str] = []
            for member in members:
                if member in seen:
                    continue
                seen.add(member)
                if len(position_ids) < batch_size:
                    position_ids.append(member)
                else:
                    overflow.append(member)
            if overflow:
                self.redis_client.rpush(
                    PNL_DIRTY_POSITION_SCAN_BUFFER_KEY,
                    *overflow,
                )
            if cursor == 0 or (
                not first_scan and cursor == start_cursor
            ):
                break
            first_scan = False

        # 在返回业务数据前保存下一次起点。本批成员持久化失败不会回滚游标，
        # 因而一个坏成员无法永久阻塞后续正常成员；集合成员本身不会被误删。
        self.redis_client.set(
            PNL_DIRTY_POSITION_SCAN_CURSOR_KEY,
            str(cursor),
        )
        position_ids = position_ids[:batch_size]
        if not position_ids:
            return []
        versions = self.redis_client.hmget(
            PNL_DIRTY_POSITION_VERSIONS_KEY,
            position_ids,
        )
        result: list[tuple[str, str]] = []
        for position_id, version in zip(
            position_ids,
            versions,
            strict=True,
        ):
            if version is None:
                # Set成员与版本Hash必须由同一Lua脚本写入。版本缺失说明是
                # 历史测试/异常中断留下的孤儿成员；只在版本仍不存在时原子
                # 删除，避免与并发的新Dirty写入竞争。
                self.redis_client.eval(
                    CLEAR_DIRTY_IF_VERSION_MISSING_SCRIPT,
                    2,
                    PNL_DIRTY_POSITION_VERSIONS_KEY,
                    PNL_DIRTY_POSITIONS_KEY,
                    position_id,
                )
                continue
            result.append((position_id, version))
        return result

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

    def list_dirty_accounts(
        self,
        batch_size: int,
    ) -> list[tuple[str, str]]:
        """
        按Redis持久化游标轮转读取账户Dirty及其CAS版本。

        账户估值可能因为行情长期缺失而持续保留Dirty。游标和溢出缓冲区
        都保存在Redis中，使一个暂时无法处理的账户不会永久占满批次头部，
        Worker重启后也能继续从上次位置扫描。
        """

        if batch_size <= 0:
            return []

        buffered = self.redis_client.eval(
            POP_DIRTY_SCAN_BUFFER_SCRIPT,
            1,
            PNL_DIRTY_ACCOUNT_SCAN_BUFFER_KEY,
            batch_size,
        ) or []
        account_ids: list[str] = list(dict.fromkeys(buffered))
        seen = set(account_ids)
        raw_cursor = self.redis_client.get(
            PNL_DIRTY_ACCOUNT_SCAN_CURSOR_KEY
        )
        try:
            cursor = int(raw_cursor or 0)
        except (TypeError, ValueError):
            cursor = 0

        start_cursor = cursor
        first_scan = True
        while len(account_ids) < batch_size:
            cursor, members = self.redis_client.sscan(
                PNL_DIRTY_ACCOUNTS_KEY,
                cursor=cursor,
                count=batch_size,
            )
            overflow: list[str] = []
            for member in members:
                if member in seen:
                    continue
                seen.add(member)
                if len(account_ids) < batch_size:
                    account_ids.append(member)
                else:
                    overflow.append(member)
            if overflow:
                self.redis_client.rpush(
                    PNL_DIRTY_ACCOUNT_SCAN_BUFFER_KEY,
                    *overflow,
                )
            if cursor == 0 or (
                not first_scan and cursor == start_cursor
            ):
                break
            first_scan = False

        self.redis_client.set(
            PNL_DIRTY_ACCOUNT_SCAN_CURSOR_KEY,
            str(cursor),
        )
        account_ids = account_ids[:batch_size]
        if not account_ids:
            return []
        versions = self.redis_client.hmget(
            PNL_DIRTY_ACCOUNT_VERSIONS_KEY,
            account_ids,
        )
        result: list[tuple[str, str]] = []
        for account_id, version in zip(
            account_ids,
            versions,
            strict=True,
        ):
            if version is None:
                self.redis_client.eval(
                    CLEAR_DIRTY_IF_VERSION_MISSING_SCRIPT,
                    2,
                    PNL_DIRTY_ACCOUNT_VERSIONS_KEY,
                    PNL_DIRTY_ACCOUNTS_KEY,
                    account_id,
                )
                continue
            result.append((account_id, version))
        return result

    def complete_dirty_account(
        self,
        account_id: str,
        expected_version: str | None = None,
    ) -> bool:
        """仅清除本次已经提交的账户版本，保留处理期间产生的新Dirty。"""

        if expected_version is None:
            return bool(
                self.redis_client.srem(PNL_DIRTY_ACCOUNTS_KEY, account_id)
            )
        return bool(
            self.redis_client.eval(
                CLEAR_DIRTY_IF_UNCHANGED_SCRIPT,
                2,
                PNL_DIRTY_ACCOUNT_VERSIONS_KEY,
                PNL_DIRTY_ACCOUNTS_KEY,
                account_id,
                expected_version,
            )
        )

    def rebuild_active_indexes(
        self,
        *,
        expected_cache_version: str,
        positions: Iterable[tuple[str, str, str, str, str]],
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
                    PNL_CONTRACT_ORDER_BOOK_IDS_KEY,
                )
                for account_id, exchange_id, symbol, order_book_id, position_id in items:
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
                    pipeline.hset(
                        PNL_CONTRACT_ORDER_BOOK_IDS_KEY,
                        contract_key,
                        order_book_id,
                    )
                pipeline.execute()
                return True
            except WatchError:
                continue
            finally:
                pipeline.reset()
        return False

    def rebuild_after_daily_settlement(
        self,
        *,
        active_positions,
        affected_positions,
        affected_account_ids=(),
        position_snapshots: Iterable[PositionRealtimePnl] = (),
        account_snapshots: Iterable[AccountRealtimePnl] = (),
        dirty_version: str = "DAILY_SETTLEMENT",
    ) -> None:
        """按日终事实直接写入下一交易日零当日盈亏基准快照。

        本方法只操作可重建派生键，不触碰行情 Stream、Consumer Group、
        Pending 消息或 Outbox。任何 Redis 异常由调用方记录为缓存待恢复，
        不能反向重做已经提交的数据库资金过账。
        """

        active = [tuple(item) for item in active_positions]
        affected = [tuple(item) for item in affected_positions]
        position_items = list(position_snapshots)
        account_items = list(account_snapshots)
        account_ids = sorted(
            {str(item[0]) for item in affected}
            | {str(account_id) for account_id in affected_account_ids}
        )
        position_ids = sorted({str(item[3]) for item in affected})
        pipeline = self.redis_client.pipeline(transaction=True)
        snapshot_keys = [pnl_account_key(account_id) for account_id in account_ids]
        snapshot_keys.extend(pnl_position_key(position_id) for position_id in position_ids)
        if snapshot_keys:
            pipeline.delete(*snapshot_keys)
        for account_id, exchange_id, symbol, position_id, expired_closed in affected:
            if expired_closed:
                pipeline.srem(
                    pnl_contract_positions_key(exchange_id, symbol),
                    position_id,
                )
                pipeline.srem(
                    pnl_account_positions_key(account_id),
                    position_id,
                )
        pipeline.execute()

        cache_version = self.bump_position_cache_version()
        if not self.rebuild_active_indexes(
            expected_cache_version=cache_version,
            positions=active,
        ):
            raise RuntimeError("日终期间持仓版本再次变化，PnL索引重建未完成")
        self.write_cycle_snapshots(
            positions=position_items,
            accounts=account_items,
            dirty_version=dirty_version,
            active_positions=active,
            closed_positions=[],
            expected_cache_version=cache_version,
            mark_dirty=False,
        )
