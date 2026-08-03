import time
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Callable, Mapping

from sqlalchemy.orm import Session

from app.repositories.account_repository import AccountRepository
from app.repositories.position_repository import PositionRepository
from app.services.pnl_calculator import (
    PnlDetailSnapshot,
    PositionPnlSnapshot,
)


ContractKey = tuple[str, str]


@dataclass(frozen=True)
class AccountPnlSnapshot:
    """账户实时盈亏汇总所需的PostgreSQL事实快照。"""

    account_id: str
    cash_balance: Decimal
    used_margin: Decimal
    frozen_margin: Decimal
    frozen_cash: Decimal
    frozen_commission: Decimal
    unrealized_pnl: Decimal
    daily_position_pnl: Decimal
    daily_close_pnl: Decimal
    daily_commission: Decimal
    option_used_margin: Decimal = Decimal("0")
    option_realtime_required_margin: Decimal = Decimal("0")
    long_option_market_value: Decimal = Decimal("0")
    short_option_market_value: Decimal = Decimal("0")
    risk_available_cash: Decimal = Decimal("0")
    risk_state: str = "NORMAL"


@dataclass(frozen=True)
class ActivePositionCycleSnapshot:
    """
    单个计算周期共享的不可变活动持仓视图。

    Worker每500ms最多创建一次该对象。同一周期内按合约、按账户和读取账户
    基础资金都使用同一份映射，避免重复读取Redis版本或PostgreSQL。
    """

    by_contract: Mapping[ContractKey, tuple[PositionPnlSnapshot, ...]]
    by_account: Mapping[str, tuple[PositionPnlSnapshot, ...]]
    accounts: Mapping[str, AccountPnlSnapshot]
    cache_version: str | None
    refresh_count: int
    by_underlying: Mapping[
        ContractKey, tuple[PositionPnlSnapshot, ...]
    ] = field(default_factory=lambda: MappingProxyType({}))

    def get_by_contract(
        self,
        exchange_id: str,
        symbol: str,
    ) -> tuple[PositionPnlSnapshot, ...]:
        key = (
            exchange_id.strip().upper(),
            symbol.strip().upper(),
        )
        return self.by_contract.get(key, ())

    def get_by_account(
        self,
        account_id: str,
    ) -> tuple[PositionPnlSnapshot, ...]:
        return self.by_account.get(account_id, ())

    def get_account(self, account_id: str) -> AccountPnlSnapshot | None:
        return self.accounts.get(account_id)

    def get_by_underlying(
        self,
        exchange_id: str,
        symbol: str,
    ) -> tuple[PositionPnlSnapshot, ...]:
        return self.by_underlying.get(
            (
                exchange_id.strip().upper(),
                symbol.strip().upper(),
            ),
            (),
        )


class ActivePositionCache:
    """
    活动持仓和账户事实的短周期不可变内存缓存。

    缓存中只保存Decimal和标量快照，不跨Session保存ORM对象。订单事实只
    增量刷新相关账户，成交事实只增量刷新相关合约；冷启动和低频对账才读取
    全部活动持仓。
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        position_repository: PositionRepository | None = None,
        account_repository: AccountRepository | None = None,
        refresh_ms: int = 1000,
        monotonic: Callable[[], float] = time.monotonic,
        version_loader: Callable[[], str] | None = None,
    ):
        self.session_factory = session_factory
        self.position_repository = (
            position_repository or PositionRepository()
        )
        self.account_repository = (
            account_repository or AccountRepository()
        )
        self.refresh_seconds = max(refresh_ms, 1) / 1000
        self.monotonic = monotonic
        self.version_loader = version_loader
        self._expires_at = 0.0
        self._by_contract: dict[
            ContractKey, tuple[PositionPnlSnapshot, ...]
        ] = {}
        self._by_account: dict[str, tuple[PositionPnlSnapshot, ...]] = {}
        self._accounts: dict[str, AccountPnlSnapshot] = {}
        self._external_version: str | None = None
        self._refresh_count = 0
        self._initialized = False
        self._pending_account_ids: set[str] = set()
        self._pending_contract_keys: set[ContractKey] = set()
        self._account_fact_versions: dict[str, str] = {}
        self._contract_fact_versions: dict[ContractKey, str] = {}

    @staticmethod
    def _account_snapshot(account) -> AccountPnlSnapshot:
        return AccountPnlSnapshot(
            account_id=account.account_id,
            cash_balance=Decimal(account.cash_balance),
            used_margin=Decimal(account.used_margin),
            frozen_margin=Decimal(account.frozen_margin),
            frozen_cash=Decimal(account.frozen_cash),
            frozen_commission=Decimal(account.frozen_commission),
            unrealized_pnl=Decimal(account.unrealized_pnl),
            daily_position_pnl=Decimal(account.daily_position_pnl),
            daily_close_pnl=Decimal(account.daily_close_pnl),
            daily_commission=Decimal(account.daily_commission),
            option_used_margin=Decimal(
                getattr(account, "option_used_margin", Decimal("0"))
            ),
            option_realtime_required_margin=Decimal(
                getattr(
                    account,
                    "option_realtime_required_margin",
                    Decimal("0"),
                )
            ),
            long_option_market_value=Decimal(
                getattr(
                    account,
                    "long_option_market_value",
                    Decimal("0"),
                )
            ),
            short_option_market_value=Decimal(
                getattr(
                    account,
                    "short_option_market_value",
                    Decimal("0"),
                )
            ),
            risk_available_cash=Decimal(
                getattr(account, "risk_available_cash", Decimal("0"))
            ),
            risk_state=getattr(account, "risk_state", "NORMAL"),
        )

    def invalidate(
        self,
        *,
        account_id: str | None = None,
        exchange_id: str | None = None,
        symbol: str | None = None,
    ) -> None:
        """记录本进程下一周期需要增量刷新的账户或合约。"""

        if account_id:
            self._pending_account_ids.add(account_id.strip())
        if exchange_id and symbol:
            self._pending_contract_keys.add(
                (
                    exchange_id.strip().upper(),
                    symbol.strip().upper(),
                )
            )
        if not account_id and not (exchange_id and symbol):
            self._expires_at = 0.0

    @staticmethod
    def _snapshots_from_rows(
        rows,
    ) -> tuple[
        dict[ContractKey, tuple[PositionPnlSnapshot, ...]],
        dict[str, AccountPnlSnapshot],
    ]:
        """把当前Session内的ORM行转换为不可变标量快照。"""

        grouped: dict[str, dict[str, object]] = {}
        accounts: dict[str, AccountPnlSnapshot] = {}
        for row in rows:
            position, detail, instrument, account = row[:4]
            underlying = row[4] if len(row) > 4 else None
            item = grouped.setdefault(
                position.position_id,
                {
                    "position": position,
                    "instrument": instrument,
                    "details": [],
                    "detail_multipliers": [],
                    "detail_rule_snapshots": [],
                    "underlying": underlying,
                },
            )
            item["details"].append(
                PnlDetailSnapshot(
                    position_detail_id=detail.position_detail_id,
                    open_price=Decimal(detail.open_price),
                    pnl_base_price=Decimal(detail.pnl_base_price),
                    remaining_volume=detail.remaining_volume,
                )
            )
            item["detail_multipliers"].append(
                Decimal(detail.multiplier_snapshot)
            )
            item["detail_rule_snapshots"].append(
                (
                    getattr(detail, "margin_rule_id", None),
                    getattr(detail, "margin_rule_version", None),
                    getattr(detail, "margin_rule_snapshot", None) or {},
                )
            )
            accounts[account.account_id] = (
                ActivePositionCache._account_snapshot(account)
            )

        by_contract: dict[
            ContractKey, list[PositionPnlSnapshot]
        ] = {}
        for item in grouped.values():
            position = item["position"]
            instrument = item["instrument"]
            underlying = item["underlying"]
            raw_rule_snapshot = (
                getattr(position, "margin_rule_snapshot", None) or {}
            )
            position_multiplier = Decimal(position.multiplier_snapshot)
            if position_multiplier <= 0 or any(
                detail_multiplier != position_multiplier
                for detail_multiplier in item["detail_multipliers"]
            ):
                raise ValueError("持仓与明细乘数快照不一致")
            if getattr(position, "instrument_type", "FUTURES") in {
                "FUTURES_OPTION",
                "INDEX_OPTION",
            } and any(
                detail_rule
                != (
                    getattr(position, "margin_rule_id", None),
                    getattr(position, "margin_rule_version", None),
                    raw_rule_snapshot,
                )
                for detail_rule in item["detail_rule_snapshots"]
            ):
                raise ValueError("期权持仓与明细保证金规则快照不一致")
            snapshot = PositionPnlSnapshot(
                position_id=position.position_id,
                account_id=position.account_id,
                order_book_id=position.order_book_id,
                exchange_id=position.exchange_id,
                symbol=position.symbol,
                direction=position.direction,
                contract_multiplier=position_multiplier,
                persisted_unrealized_pnl=Decimal(
                    position.unrealized_pnl
                ),
                persisted_daily_position_pnl=Decimal(
                    position.daily_position_pnl
                ),
                details=tuple(item["details"]),
                instrument_type=getattr(
                    position, "instrument_type", "FUTURES"
                ),
                total_volume=getattr(
                    position,
                    "total_volume",
                    sum(
                        detail.remaining_volume
                        for detail in item["details"]
                    ),
                ),
                persisted_realtime_required_margin=Decimal(
                    getattr(
                        position,
                        "realtime_required_margin",
                        Decimal("0"),
                    )
                ),
                persisted_used_margin=Decimal(
                    getattr(position, "used_margin", Decimal("0"))
                ),
                option_type=getattr(instrument, "option_type", None),
                strike_price=(
                    Decimal(instrument.strike_price)
                    if getattr(instrument, "strike_price", None) is not None
                    else None
                ),
                underlying_exchange_id=(
                    underlying.exchange_id if underlying is not None else None
                ),
                underlying_symbol=(
                    underlying.symbol if underlying is not None else None
                ),
                margin_rule_snapshot=tuple(
                    sorted(
                        (str(key), str(value))
                        for key, value in raw_rule_snapshot.items()
                    )
                ),
            )
            key = (
                position.exchange_id.strip().upper(),
                position.symbol.strip().upper(),
            )
            by_contract.setdefault(key, []).append(snapshot)
        return (
            {
                key: tuple(value)
                for key, value in by_contract.items()
            },
            accounts,
        )

    def _rebuild_by_account(self) -> None:
        """从合约索引在内存中重建账户索引，不访问数据库。"""

        by_account: dict[str, list[PositionPnlSnapshot]] = {}
        for positions in self._by_contract.values():
            for position in positions:
                by_account.setdefault(
                    position.account_id,
                    [],
                ).append(position)
        self._by_account = {
            account_id: tuple(positions)
            for account_id, positions in by_account.items()
        }

    def _full_reload(
        self,
        *,
        now: float,
        external_version: str | None,
        extra_account_ids: set[str],
    ) -> None:
        with self.session_factory() as db:
            rows = self.position_repository.list_active_calculation_rows(db)
            active_account_ids = {row[3].account_id for row in rows}
            missing_account_ids = extra_account_ids - active_account_ids
            extra_accounts = self.account_repository.list_by_account_ids(
                db,
                tuple(missing_account_ids),
            )

        by_contract, accounts = self._snapshots_from_rows(rows)
        for account in extra_accounts:
            accounts[account.account_id] = self._account_snapshot(account)

        self._by_contract = by_contract
        self._rebuild_by_account()
        self._accounts = accounts
        self._expires_at = now + self.refresh_seconds
        self._external_version = external_version
        self._refresh_count += 1
        self._initialized = True
        # 完整对账后只需保留本轮仍在Dirty集合中的版本；历史合约和账户的
        # 本地去重记录可以丢弃，避免Worker长期运行时字典持续增长。
        self._account_fact_versions.clear()
        self._contract_fact_versions.clear()

    def _incremental_refresh(
        self,
        *,
        account_ids: set[str],
        contract_keys: set[ContractKey],
        external_version: str | None,
    ) -> None:
        """
        用一次数据库Session定向刷新账户事实和合约持仓结构。

        多个账户使用一次批量账户查询，多个合约使用一次联表SQL；全部平仓时
        指定合约返回空行，旧合约快照仍会被明确删除。
        """

        with self.session_factory() as db:
            rows = (
                self.position_repository
                .list_active_calculation_rows_by_contracts(
                    db,
                    tuple(contract_keys),
                )
                if contract_keys
                else []
            )
            accounts = self.account_repository.list_by_account_ids(
                db,
                tuple(sorted(account_ids)),
            )

        refreshed_contracts, row_accounts = (
            self._snapshots_from_rows(rows)
        )
        if contract_keys:
            for key in contract_keys:
                self._by_contract.pop(key, None)
            self._by_contract.update(refreshed_contracts)
            self._rebuild_by_account()

        found_account_ids = set()
        for account in accounts:
            found_account_ids.add(account.account_id)
            self._accounts[account.account_id] = (
                self._account_snapshot(account)
            )
        for account_id, snapshot in row_accounts.items():
            found_account_ids.add(account_id)
            self._accounts[account_id] = snapshot
        for account_id in account_ids - found_account_ids:
            self._accounts.pop(account_id, None)

        self._external_version = external_version
        self._refresh_count += 1

    def get_cycle_snapshot(
        self,
        *,
        extra_account_ids: set[str] | None = None,
        refresh_account_ids: set[str] | None = None,
        refresh_contract_keys: set[ContractKey] | None = None,
        refresh_account_versions: Mapping[str, str] | None = None,
        refresh_contract_versions: Mapping[ContractKey, str] | None = None,
        force_refresh: bool = False,
    ) -> ActivePositionCycleSnapshot:
        """
        创建本周期不可变视图，并且只调用一次跨进程版本读取。

        全部平仓后账户不再出现在活动持仓联表结果中，所以成交Dirty携带的
        账户编号通过extra_account_ids补齐，确保可以把账户旧浮盈减为零。
        """

        now = self.monotonic()
        external_version = (
            self.version_loader()
            if self.version_loader is not None
            else None
        )
        required_accounts = {
            account_id.strip()
            for account_id in (extra_account_ids or ())
            if account_id and account_id.strip()
        }
        account_versions = {
            account_id.strip(): str(version)
            for account_id, version in (
                refresh_account_versions or {}
            ).items()
            if account_id and account_id.strip()
        }
        contract_versions = {
            (
                exchange_id.strip().upper(),
                symbol.strip().upper(),
            ): str(version)
            for (exchange_id, symbol), version in (
                refresh_contract_versions or {}
            ).items()
        }
        changed_account_versions = {
            account_id
            for account_id, version in account_versions.items()
            if self._account_fact_versions.get(account_id) != version
        }
        changed_contract_versions = {
            key
            for key, version in contract_versions.items()
            if self._contract_fact_versions.get(key) != version
        }
        requested_accounts = (
            {
                account_id.strip()
                for account_id in (refresh_account_ids or ())
                if account_id and account_id.strip()
            }
            | changed_account_versions
            | self._pending_account_ids
        )
        requested_contracts = {
            (
                exchange_id.strip().upper(),
                symbol.strip().upper(),
            )
            for exchange_id, symbol in (
                set(refresh_contract_keys or ())
                | changed_contract_versions
                | self._pending_contract_keys
            )
        }
        missing_required = required_accounts - self._accounts.keys()
        needs_full_reload = (
            force_refresh
            or not self._initialized
            or now >= self._expires_at
            or (
                external_version != self._external_version
                and not requested_contracts
            )
        )
        if needs_full_reload:
            self._full_reload(
                now=now,
                external_version=external_version,
                extra_account_ids=(
                    required_accounts | requested_accounts
                ),
            )
        elif requested_accounts or requested_contracts or missing_required:
            self._incremental_refresh(
                account_ids=requested_accounts | missing_required,
                contract_keys=requested_contracts,
                external_version=external_version,
            )
        elif external_version != self._external_version:
            # 结构Dirty会在下一轮通过Redis集合再次传入；先记录当前版本，
            # 避免同一500ms周期内重复检查触发全量查询。
            self._external_version = external_version

        self._pending_account_ids.difference_update(requested_accounts)
        self._pending_contract_keys.difference_update(requested_contracts)
        self._account_fact_versions.update(account_versions)
        self._contract_fact_versions.update(contract_versions)

        by_underlying_mutable: dict[
            ContractKey, list[PositionPnlSnapshot]
        ] = {}
        for positions in self._by_contract.values():
            for position in positions:
                if position.underlying_key is not None:
                    by_underlying_mutable.setdefault(
                        position.underlying_key, []
                    ).append(position)

        return ActivePositionCycleSnapshot(
            # 每个周期复制最外层映射，避免下一轮增量刷新改变已经交给当前
            # 计算周期的视图；内部Position/Account对象本身均为冻结快照。
            by_contract=MappingProxyType(dict(self._by_contract)),
            by_account=MappingProxyType(dict(self._by_account)),
            accounts=MappingProxyType(dict(self._accounts)),
            cache_version=self._external_version,
            refresh_count=self._refresh_count,
            by_underlying=MappingProxyType(
                {
                    key: tuple(positions)
                    for key, positions in by_underlying_mutable.items()
                }
            ),
        )

    # 下列兼容方法供查询服务和旧测试使用；实时Worker每轮只调用上面的周期接口。
    def get_by_contract(
        self,
        exchange_id: str,
        symbol: str,
    ) -> tuple[PositionPnlSnapshot, ...]:
        return self.get_cycle_snapshot().get_by_contract(
            exchange_id,
            symbol,
        )

    def get_by_account(
        self,
        account_id: str,
    ) -> tuple[PositionPnlSnapshot, ...]:
        return self.get_cycle_snapshot().get_by_account(account_id)

    def get_account(self, account_id: str) -> AccountPnlSnapshot | None:
        return self.get_cycle_snapshot(
            extra_account_ids={account_id}
        ).get_account(account_id)
