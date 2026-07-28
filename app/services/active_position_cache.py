import time
from dataclasses import dataclass
from decimal import Decimal
from threading import RLock
from typing import Callable

from sqlalchemy.orm import Session

from app.repositories.position_repository import PositionRepository
from app.services.pnl_calculator import (
    PnlDetailSnapshot,
    PositionPnlSnapshot,
)


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


class ActivePositionCache:
    """
    活动持仓短周期内存缓存。

    缓存只保存不可变Decimal和标量快照，不保存ORM对象。一次刷新读取全部
    活动持仓，随后可以按合约和账户索引，正常高频Tick不会重复查询数据库。
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        position_repository: PositionRepository | None = None,
        refresh_ms: int = 1000,
        monotonic: Callable[[], float] = time.monotonic,
        version_loader: Callable[[], str] | None = None,
    ):
        self.session_factory = session_factory
        self.position_repository = (
            position_repository or PositionRepository()
        )
        self.refresh_seconds = max(refresh_ms, 1) / 1000
        self.monotonic = monotonic
        self.version_loader = version_loader
        self._lock = RLock()
        self._expires_at = 0.0
        self._by_contract: dict[
            tuple[str, str], tuple[PositionPnlSnapshot, ...]
        ] = {}
        self._by_account: dict[str, tuple[PositionPnlSnapshot, ...]] = {}
        self._accounts: dict[str, AccountPnlSnapshot] = {}
        self._external_version: str | None = None

    def invalidate(
        self,
        *,
        account_id: str | None = None,
        exchange_id: str | None = None,
        symbol: str | None = None,
    ) -> None:
        """成交提交后的事件使缓存立即过期；参数保留用于日志和未来细分失效。"""

        _ = account_id, exchange_id, symbol
        with self._lock:
            self._expires_at = 0.0

    def _refresh_if_needed(self) -> None:
        now = self.monotonic()
        external_version = (
            self.version_loader()
            if self.version_loader is not None
            else None
        )
        with self._lock:
            if (
                now < self._expires_at
                and external_version == self._external_version
            ):
                return
            with self.session_factory() as db:
                rows = self.position_repository.list_active_calculation_rows(
                    db
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
                accounts[account.account_id] = AccountPnlSnapshot(
                    account_id=account.account_id,
                    cash_balance=Decimal(account.cash_balance),
                    used_margin=Decimal(account.used_margin),
                    frozen_margin=Decimal(account.frozen_margin),
                    frozen_cash=Decimal(account.frozen_cash),
                    frozen_commission=Decimal(
                        account.frozen_commission
                    ),
                    unrealized_pnl=Decimal(account.unrealized_pnl),
                    daily_position_pnl=Decimal(
                        account.daily_position_pnl
                    ),
                    daily_close_pnl=Decimal(account.daily_close_pnl),
                    daily_commission=Decimal(account.daily_commission),
                )

            by_contract: dict[
                tuple[str, str], list[PositionPnlSnapshot]
            ] = {}
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
                by_account.setdefault(
                    position.account_id,
                    [],
                ).append(snapshot)

            self._by_contract = {
                key: tuple(value)
                for key, value in by_contract.items()
            }
            self._by_account = {
                key: tuple(value)
                for key, value in by_account.items()
            }
            self._accounts = accounts
            self._expires_at = now + self.refresh_seconds
            self._external_version = external_version

    def get_by_contract(
        self,
        exchange_id: str,
        symbol: str,
    ) -> tuple[PositionPnlSnapshot, ...]:
        self._refresh_if_needed()
        key = (
            exchange_id.strip().upper(),
            symbol.strip().upper(),
        )
        with self._lock:
            return self._by_contract.get(key, ())

    def get_by_account(
        self,
        account_id: str,
    ) -> tuple[PositionPnlSnapshot, ...]:
        self._refresh_if_needed()
        with self._lock:
            return self._by_account.get(account_id, ())

    def get_account(
        self,
        account_id: str,
    ) -> AccountPnlSnapshot | None:
        self._refresh_if_needed()
        with self._lock:
            return self._accounts.get(account_id)
