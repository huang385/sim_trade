import time
from dataclasses import dataclass
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


class ActivePositionCache:
    """
    活动持仓的短周期不可变内存缓存。

    缓存中只保存Decimal和标量快照，不跨Session保存ORM对象。成交进程递增
    Redis缓存版本后，实时盈亏Worker下一周期只检查一次版本，并在需要时一次
    性重建全部索引。
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
        )

    def invalidate(
        self,
        *,
        account_id: str | None = None,
        exchange_id: str | None = None,
        symbol: str | None = None,
    ) -> None:
        """让本进程下一周期重建缓存；参数保留用于调用语义和日志。"""

        _ = account_id, exchange_id, symbol
        self._expires_at = 0.0

    def _reload(
        self,
        *,
        now: float,
        external_version: str | None,
        extra_account_ids: set[str],
    ) -> None:
        with self.session_factory() as db:
            rows = self.position_repository.list_active_calculation_rows(db)
            active_account_ids = {
                account.account_id for *_items, account in rows
            }
            missing_account_ids = extra_account_ids - active_account_ids
            extra_accounts = self.account_repository.list_by_account_ids(
                db,
                tuple(missing_account_ids),
            )

        grouped: dict[str, dict[str, object]] = {}
        accounts: dict[str, AccountPnlSnapshot] = {}
        for position, detail, instrument, account in rows:
            item = grouped.setdefault(
                position.position_id,
                {
                    "position": position,
                    "instrument": instrument,
                    "details": [],
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
            accounts[account.account_id] = self._account_snapshot(account)
        for account in extra_accounts:
            accounts[account.account_id] = self._account_snapshot(account)

        by_contract: dict[ContractKey, list[PositionPnlSnapshot]] = {}
        by_account: dict[str, list[PositionPnlSnapshot]] = {}
        for item in grouped.values():
            position = item["position"]
            instrument = item["instrument"]
            snapshot = PositionPnlSnapshot(
                position_id=position.position_id,
                account_id=position.account_id,
                order_book_id=position.order_book_id,
                exchange_id=position.exchange_id,
                symbol=position.symbol,
                direction=position.direction,
                contract_multiplier=Decimal(
                    instrument.contract_multiplier
                ),
                persisted_unrealized_pnl=Decimal(
                    position.unrealized_pnl
                ),
                persisted_daily_position_pnl=Decimal(
                    position.daily_position_pnl
                ),
                details=tuple(item["details"]),
            )
            key = (
                position.exchange_id.strip().upper(),
                position.symbol.strip().upper(),
            )
            by_contract.setdefault(key, []).append(snapshot)
            by_account.setdefault(position.account_id, []).append(snapshot)

        self._by_contract = {
            key: tuple(value) for key, value in by_contract.items()
        }
        self._by_account = {
            key: tuple(value) for key, value in by_account.items()
        }
        self._accounts = accounts
        self._expires_at = now + self.refresh_seconds
        self._external_version = external_version
        self._refresh_count += 1

    def get_cycle_snapshot(
        self,
        *,
        extra_account_ids: set[str] | None = None,
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
        required_accounts = set(extra_account_ids or ())
        missing_required = required_accounts - self._accounts.keys()
        if (
            force_refresh
            or now >= self._expires_at
            or external_version != self._external_version
            or bool(missing_required)
        ):
            self._reload(
                now=now,
                external_version=external_version,
                extra_account_ids=required_accounts,
            )

        return ActivePositionCycleSnapshot(
            by_contract=MappingProxyType(self._by_contract),
            by_account=MappingProxyType(self._by_account),
            accounts=MappingProxyType(self._accounts),
            cache_version=self._external_version,
            refresh_count=self._refresh_count,
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
