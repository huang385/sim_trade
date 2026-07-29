import json
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping

from app.common.decimal_utils import quantize_money
from app.common.time_utils import utc_now
from app.infrastructure.market_data.market_tick_store import MarketTickStore
from app.infrastructure.realtime_pnl_store import RealtimePnlStore
from app.schemas.market_tick_schema import MarketTick
from app.schemas.pnl_schema import (
    AccountRealtimePnl,
    PositionRealtimePnl,
)
from app.services.active_position_cache import (
    ActivePositionCache,
    ActivePositionCycleSnapshot,
)
from app.services.pnl_calculator import (
    PnlCalculator,
    PositionPnlResult,
    PositionPnlSnapshot,
)


logger = logging.getLogger(__name__)
RISK_QUANT = Decimal("0.00000001")
ContractKey = tuple[str, str]


class PnlEventValidationError(ValueError):
    """行情消息结构固定错误，继续重试不会恢复。"""


class PnlWorkerLeaseLostError(RuntimeError):
    """最终写入前租约已经失效，本轮结果必须丢弃并保留Pending。"""


@dataclass(frozen=True)
class ContractPnlRequest:
    """单个合约在一个500ms周期内的最终计算输入。"""

    exchange_id: str
    symbol: str
    tick: MarketTick | None
    dirty_version: str | None = None
    dirty_account_ids: frozenset[str] = frozenset()

    @property
    def key(self) -> ContractKey:
        return (
            self.exchange_id.strip().upper(),
            self.symbol.strip().upper(),
        )


@dataclass(frozen=True)
class RealtimePnlProcessResult:
    """单条行情兼容入口或单批次的实时盈亏处理统计。"""

    action: str
    positions_calculated: int = 0
    redis_snapshots_written: int = 0
    dirty_positions: int = 0
    accounts_updated: int = 0
    successful_contracts: frozenset[ContractKey] = frozenset()
    failed_contracts: frozenset[ContractKey] = frozenset()
    no_position_contracts: frozenset[ContractKey] = frozenset()
    reconciled_accounts: int = 0


class RealtimePnlService:
    """
    以单周期不可变持仓快照批量计算实时PnL。

    本服务是pnl:position和pnl:account的唯一业务写入者。金额先在Python中
    使用Decimal算出绝对值，再由Redis事务Pipeline一次写入；PostgreSQL由
    独立持久化Worker更新。
    """

    def __init__(
        self,
        *,
        active_position_cache: ActivePositionCache,
        pnl_store: RealtimePnlStore,
        calculator: PnlCalculator | None = None,
        market_tick_store: MarketTickStore | None = None,
    ):
        self.active_position_cache = active_position_cache
        self.pnl_store = pnl_store
        self.calculator = calculator or PnlCalculator()
        self.market_tick_store = market_tick_store

    @staticmethod
    def parse_tick(fields: Mapping[str, str]) -> MarketTick | None:
        """解析实时行情；REST快照和空价格由调用方直接确认。"""

        if fields.get("event_type", "").strip() != "MARKET_TICK":
            raise PnlEventValidationError(
                "PnL消费者只支持MARKET_TICK事件"
            )
        try:
            payload = json.loads(fields.get("payload", ""))
        except (TypeError, json.JSONDecodeError) as exc:
            raise PnlEventValidationError(
                "行情payload不是合法JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise PnlEventValidationError("行情payload必须是对象")
        if (
            payload.get("source") != "YML_FEEDHUB"
            or payload.get("ingest_type") != "LIVE_CALLBACK"
        ):
            return None
        try:
            return MarketTick.model_validate(payload)
        except Exception as exc:
            raise PnlEventValidationError(
                "实时行情字段校验失败"
            ) from exc

    @staticmethod
    def _decimal(
        values: Mapping[str, str],
        key: str,
        fallback: Decimal,
    ) -> Decimal:
        raw = values.get(key)
        return Decimal(raw) if raw not in (None, "") else fallback

    @staticmethod
    def _risk_ratio(
        used_margin: Decimal,
        equity: Decimal,
    ) -> Decimal:
        if equity <= 0:
            return Decimal("0.00000000")
        return (used_margin / equity).quantize(
            RISK_QUANT,
            rounding=ROUND_HALF_UP,
        )

    @staticmethod
    def _position_model(
        *,
        snapshot: PositionPnlSnapshot,
        result: PositionPnlResult,
        tick: MarketTick,
        updated_at: datetime,
    ) -> PositionRealtimePnl:
        return PositionRealtimePnl(
            position_id=snapshot.position_id,
            account_id=snapshot.account_id,
            exchange_id=snapshot.exchange_id,
            symbol=snapshot.symbol,
            direction=snapshot.direction,
            mark_price=tick.last_price,
            cumulative_unrealized_pnl=(
                result.cumulative_unrealized_pnl
            ),
            daily_position_pnl=result.daily_position_pnl,
            event_time=tick.event_time,
            source_event_id=tick.source_event_id,
            updated_at=updated_at,
        )

    def _calculate_missing_position(
        self,
        position: PositionPnlSnapshot,
        *,
        latest_by_key: Mapping[ContractKey, MarketTick | None],
    ) -> PositionPnlResult:
        """
        冷启动快照缺失时用Redis最新行情完整重算。

        如果没有有效行情，退回PostgreSQL最近快照；不会把已有浮盈错误当成0。
        """

        key = (
            position.exchange_id.strip().upper(),
            position.symbol.strip().upper(),
        )
        tick = latest_by_key.get(key)
        if tick is not None and tick.last_price is not None and tick.last_price > 0:
            return self.calculator.calculate_position(
                mark_price=tick.last_price,
                snapshot=position,
            )
        return PositionPnlResult(
            cumulative_unrealized_pnl=(
                position.persisted_unrealized_pnl
            ),
            daily_position_pnl=position.persisted_daily_position_pnl,
        )

    def process_batch(
        self,
        *,
        requests: list[ContractPnlRequest],
        cycle_snapshot: ActivePositionCycleSnapshot,
        dirty_version: str,
        force_reconciliation: bool = False,
        lease_owner: str | None = None,
    ) -> RealtimePnlProcessResult:
        """计算多个合约，并在同一批次内每个账户只生成一个最终快照。"""

        if not requests:
            return RealtimePnlProcessResult(action="SKIPPED")

        updated_at = utc_now()
        current_by_key = {
            request.key: cycle_snapshot.get_by_contract(*request.key)
            for request in requests
        }
        indexed_ids = self.pnl_store.list_contract_position_ids_many(
            request.key for request in requests
        )
        all_position_ids: set[str] = set()
        for key, positions in current_by_key.items():
            all_position_ids.update(item.position_id for item in positions)
            all_position_ids.update(indexed_ids[key])
        old_positions = self.pnl_store.get_positions_many(all_position_ids)

        calculated: dict[str, PositionPnlResult] = {}
        position_models: dict[str, PositionRealtimePnl] = {}
        account_deltas: dict[str, list[Decimal]] = {}
        affected_accounts: set[str] = set()
        active_index_additions: list[tuple[str, str, str, str]] = []
        closed_index_removals: list[tuple[str, str, str, str]] = []
        successful: set[ContractKey] = set()
        failed: set[ContractKey] = set()
        no_position: set[ContractKey] = set()
        accounts_by_contract: dict[ContractKey, set[str]] = {}

        for request in requests:
            key = request.key
            positions = current_by_key[key]
            current_ids = {item.position_id for item in positions}
            old_ids = indexed_ids[key]
            closed_ids = old_ids - current_ids
            contract_accounts = set(request.dirty_account_ids)

            # 有活动持仓时必须有有效最新价；Dirty保留等待后续行情恢复。
            if positions and (
                request.tick is None
                or request.tick.last_price is None
                or request.tick.last_price <= 0
            ):
                failed.add(key)
                continue

            try:
                local_calculated: dict[str, PositionPnlResult] = {}
                local_models: dict[str, PositionRealtimePnl] = {}
                local_deltas: dict[str, list[Decimal]] = {}
                local_additions: list[
                    tuple[str, str, str, str]
                ] = []
                local_removals: list[
                    tuple[str, str, str, str]
                ] = []
                for position in positions:
                    result = self.calculator.calculate_position(
                        mark_price=request.tick.last_price,
                        snapshot=position,
                    )
                    local_calculated[position.position_id] = result
                    local_models[position.position_id] = (
                        self._position_model(
                            snapshot=position,
                            result=result,
                            tick=request.tick,
                            updated_at=updated_at,
                        )
                    )
                    old = old_positions.get(position.position_id, {})
                    old_cumulative = self._decimal(
                        old,
                        "cumulative_unrealized_pnl",
                        position.persisted_unrealized_pnl,
                    )
                    old_daily = self._decimal(
                        old,
                        "daily_position_pnl",
                        position.persisted_daily_position_pnl,
                    )
                    delta = local_deltas.setdefault(
                        position.account_id,
                        [Decimal("0"), Decimal("0")],
                    )
                    delta[0] += (
                        result.cumulative_unrealized_pnl
                        - old_cumulative
                    )
                    delta[1] += result.daily_position_pnl - old_daily
                    contract_accounts.add(position.account_id)
                    if position.position_id not in old_ids:
                        local_additions.append(
                            (
                                position.account_id,
                                position.exchange_id,
                                position.symbol,
                                position.position_id,
                            )
                        )

                for position_id in closed_ids:
                    old = old_positions.get(position_id, {})
                    if not old:
                        continue
                    account_id = old.get("account_id", "")
                    if not account_id:
                        continue
                    old_cumulative = self._decimal(
                        old,
                        "cumulative_unrealized_pnl",
                        Decimal("0"),
                    )
                    old_daily = self._decimal(
                        old,
                        "daily_position_pnl",
                        Decimal("0"),
                    )
                    delta = local_deltas.setdefault(
                        account_id,
                        [Decimal("0"), Decimal("0")],
                    )
                    delta[0] -= old_cumulative
                    delta[1] -= old_daily
                    contract_accounts.add(account_id)
                    event_time = (
                        request.tick.event_time
                        if request.tick is not None
                        else updated_at
                    )
                    mark_price = self._decimal(
                        old,
                        "mark_price",
                        (
                            request.tick.last_price
                            if request.tick is not None
                            and request.tick.last_price is not None
                            else Decimal("0")
                        ),
                    )
                    local_models[position_id] = PositionRealtimePnl(
                        position_id=position_id,
                        account_id=account_id,
                        exchange_id=old.get("exchange_id", key[0]),
                        symbol=old.get("symbol", key[1]),
                        direction=old.get("direction", ""),
                        mark_price=mark_price,
                        cumulative_unrealized_pnl=Decimal("0.000000"),
                        daily_position_pnl=Decimal("0.000000"),
                        event_time=event_time,
                        source_event_id=(
                            request.tick.source_event_id
                            if request.tick is not None
                            else f"DIRTY-{request.dirty_version or '0'}"
                        ),
                        updated_at=updated_at,
                    )
                    local_removals.append(
                        (
                            account_id,
                            key[0],
                            key[1],
                            position_id,
                        )
                    )
                if not positions and not closed_ids:
                    no_position.add(key)
                calculated.update(local_calculated)
                position_models.update(local_models)
                for account_id, values in local_deltas.items():
                    delta = account_deltas.setdefault(
                        account_id,
                        [Decimal("0"), Decimal("0")],
                    )
                    delta[0] += values[0]
                    delta[1] += values[1]
                active_index_additions.extend(local_additions)
                closed_index_removals.extend(local_removals)
                affected_accounts.update(contract_accounts)
                accounts_by_contract[key] = contract_accounts
                successful.add(key)
            except Exception:
                logger.exception(
                    "合约实时PnL计算失败 contract=%s:%s",
                    *key,
                )
                failed.add(key)

        # 失败合约不会产生快照，也不会被Worker ACK或清除Dirty。
        successful -= failed
        affected_accounts = {
            account_id
            for key in successful
            for account_id in accounts_by_contract.get(key, ())
        }
        old_accounts = self.pnl_store.get_accounts_many(affected_accounts)

        # 完整汇总需要批量读到账户全部活动持仓。普通周期仅在账户快照缺失时
        # 进入该分支，作为冷启动恢复。
        dirty_account_ids = {
            account_id
            for request in requests
            for account_id in request.dirty_account_ids
        }
        full_accounts = {
            account_id
            for account_id in affected_accounts
            if force_reconciliation
            or not old_accounts.get(account_id)
            or account_id in dirty_account_ids
        }
        full_position_ids = {
            position.position_id
            for account_id in full_accounts
            for position in cycle_snapshot.get_by_account(account_id)
        }
        missing_old = full_position_ids - old_positions.keys()
        if missing_old:
            old_positions.update(
                self.pnl_store.get_positions_many(missing_old)
            )

        # 完整对账可能需要补算本轮请求之外的同账户持仓。先对相关合约去重，
        # 再用一个Pipeline读取最新行情，禁止逐持仓访问Redis。
        latest_ticks: dict[ContractKey, MarketTick | None] = {
            request.key: request.tick for request in requests
        }
        missing_contract_keys = {
            (
                position.exchange_id.strip().upper(),
                position.symbol.strip().upper(),
            )
            for account_id in full_accounts
            for position in cycle_snapshot.get_by_account(account_id)
            if (
                position.exchange_id.strip().upper(),
                position.symbol.strip().upper(),
            )
            not in latest_ticks
        }
        if missing_contract_keys and self.market_tick_store is not None:
            latest_rows = self.market_tick_store.get_latest_many(
                missing_contract_keys
            )
            for key, latest in latest_rows.items():
                try:
                    tick = (
                        MarketTickStore.mapping_to_tick(latest)
                        if latest
                        else None
                    )
                except Exception:
                    tick = None
                    logger.warning(
                        "冷启动PnL行情不可用 contract=%s:%s",
                        *key,
                    )
                latest_ticks[key] = tick

        account_models: list[AccountRealtimePnl] = []
        reconciled_accounts = 0
        for account_id in affected_accounts:
            account = cycle_snapshot.get_account(account_id)
            if account is None:
                failed.update(
                    key
                    for key in successful
                    if account_id in accounts_by_contract.get(key, ())
                )
                continue
            if account_id in full_accounts:
                cumulative = Decimal("0")
                daily_position = Decimal("0")
                for position in cycle_snapshot.get_by_account(account_id):
                    result = calculated.get(position.position_id)
                    if result is None:
                        old = old_positions.get(position.position_id, {})
                        if old:
                            result = PositionPnlResult(
                                cumulative_unrealized_pnl=self._decimal(
                                    old,
                                    "cumulative_unrealized_pnl",
                                    position.persisted_unrealized_pnl,
                                ),
                                daily_position_pnl=self._decimal(
                                    old,
                                    "daily_position_pnl",
                                    position.persisted_daily_position_pnl,
                                ),
                            )
                        else:
                            result = self._calculate_missing_position(
                                position,
                                latest_by_key=latest_ticks,
                            )
                    cumulative += result.cumulative_unrealized_pnl
                    daily_position += result.daily_position_pnl
                reconciled_accounts += 1
                previous = old_accounts.get(account_id, {})
                if previous and (
                    self._decimal(
                        previous,
                        "cumulative_unrealized_pnl",
                        cumulative,
                    )
                    != quantize_money(cumulative)
                    or self._decimal(
                        previous,
                        "daily_position_pnl",
                        daily_position,
                    )
                    != quantize_money(daily_position)
                ):
                    logger.warning(
                        "账户实时PnL完整对账修正 account_id=%s",
                        account_id,
                    )
            else:
                old = old_accounts[account_id]
                delta = account_deltas.get(
                    account_id,
                    [Decimal("0"), Decimal("0")],
                )
                cumulative = self._decimal(
                    old,
                    "cumulative_unrealized_pnl",
                    account.unrealized_pnl,
                ) + delta[0]
                daily_position = self._decimal(
                    old,
                    "daily_position_pnl",
                    account.daily_position_pnl,
                ) + delta[1]

            cumulative = quantize_money(cumulative)
            daily_position = quantize_money(daily_position)
            daily_pnl = quantize_money(
                daily_position
                + account.daily_close_pnl
                - account.daily_commission
            )
            equity = quantize_money(
                account.cash_balance + cumulative
            )
            available_cash = quantize_money(
                equity
                - account.used_margin
                - account.frozen_margin
                - account.frozen_cash
                - account.frozen_commission
            )
            account_models.append(
                AccountRealtimePnl(
                    account_id=account_id,
                    cumulative_unrealized_pnl=cumulative,
                    daily_position_pnl=daily_position,
                    daily_close_pnl=account.daily_close_pnl,
                    daily_commission=account.daily_commission,
                    daily_pnl=daily_pnl,
                    equity=equity,
                    available_cash=available_cash,
                    risk_ratio=self._risk_ratio(
                        account.used_margin,
                        equity,
                    ),
                    updated_at=updated_at,
                )
            )

        successful -= failed
        # 如果某合约后续因账户事实缺失失败，排除它产生的快照，避免部分更新。
        allowed_position_ids = {
            position.position_id
            for key in successful
            for position in current_by_key[key]
        } | {
            position_id
            for key in successful
            for position_id in (indexed_ids[key] - {
                item.position_id for item in current_by_key[key]
            })
        }
        models = [
            model
            for position_id, model in position_models.items()
            if position_id in allowed_position_ids
        ]
        if models or account_models:
            active_positions = [
                item
                for item in active_index_additions
                if (item[1], item[2]) in successful
            ]
            closed_positions = [
                item
                for item in closed_index_removals
                if (item[1], item[2]) in successful
            ]
            if lease_owner is None:
                written_positions, written_accounts = (
                    self.pnl_store.write_cycle_snapshots(
                        positions=models,
                        accounts=account_models,
                        dirty_version=dirty_version,
                        active_positions=active_positions,
                        closed_positions=closed_positions,
                    )
                )
            else:
                lease_owned, written_positions, written_accounts = (
                    self.pnl_store.write_cycle_snapshots_if_lease_owned(
                        lease_owner=lease_owner,
                        positions=models,
                        accounts=account_models,
                        dirty_version=dirty_version,
                        active_positions=active_positions,
                        closed_positions=closed_positions,
                    )
                )
                if not lease_owned:
                    raise PnlWorkerLeaseLostError(
                        "实时PnL最终写入前租约已失效"
                    )
        else:
            written_positions = written_accounts = 0

        return RealtimePnlProcessResult(
            action="CALCULATED" if successful else "SKIPPED",
            positions_calculated=len(models),
            redis_snapshots_written=(
                written_positions + written_accounts
            ),
            dirty_positions=written_positions,
            accounts_updated=written_accounts,
            successful_contracts=frozenset(successful),
            failed_contracts=frozenset(failed),
            no_position_contracts=frozenset(no_position),
            reconciled_accounts=reconciled_accounts,
        )

    def process(
        self,
        *,
        stream_message_id: str,
        fields: Mapping[str, str],
    ) -> RealtimePnlProcessResult:
        """保留单条调用兼容性；生产Worker使用process_batch。"""

        tick = self.parse_tick(fields)
        if tick is None or tick.last_price is None or tick.last_price <= 0:
            return RealtimePnlProcessResult(action="SKIPPED")
        cycle = self.active_position_cache.get_cycle_snapshot()
        request = ContractPnlRequest(
            exchange_id=tick.exchange_id,
            symbol=tick.symbol,
            tick=tick,
        )
        return self.process_batch(
            requests=[request],
            cycle_snapshot=cycle,
            dirty_version=f"{stream_message_id}:{tick.source_event_id}",
        )
