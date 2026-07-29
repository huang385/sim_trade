from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from app.common.decimal_utils import quantize_money
from app.common.time_utils import utc_now
from app.infrastructure.market_data.market_tick_store import MarketTickStore
from app.infrastructure.realtime_pnl_store import RealtimePnlStore
from app.repositories.account_repository import AccountRepository
from app.repositories.instrument_repository import InstrumentRepository
from app.repositories.position_repository import PositionRepository
from app.services.pnl_calculator import (
    PnlCalculator,
    PnlDetailSnapshot,
    PositionPnlResult,
    PositionPnlSnapshot,
)


RISK_QUANT = Decimal("0.00000001")


@dataclass(frozen=True)
class PnlPersistenceResult:
    """一轮Dirty持仓批量持久化统计。"""

    requested: int = 0
    positions_persisted: int = 0
    accounts_persisted: int = 0
    retained: int = 0


class PnlSnapshotPersistenceService:
    """
    按账户事务重新读取PostgreSQL事实并持久化最终盈亏快照。

    Redis中的持仓数量永远不作为结算依据；每次都锁定账户、持仓和有效
    PositionDetail，再读取Redis最新行情执行纯Decimal重算。
    """

    def __init__(
        self,
        *,
        session_factory,
        pnl_store: RealtimePnlStore,
        market_tick_store: MarketTickStore,
        account_repository: AccountRepository | None = None,
        position_repository: PositionRepository | None = None,
        instrument_repository: InstrumentRepository | None = None,
        calculator: PnlCalculator | None = None,
    ):
        self.session_factory = session_factory
        self.pnl_store = pnl_store
        self.market_tick_store = market_tick_store
        self.account_repository = (
            account_repository or AccountRepository()
        )
        self.position_repository = (
            position_repository or PositionRepository()
        )
        self.instrument_repository = (
            instrument_repository or InstrumentRepository()
        )
        self.calculator = calculator or PnlCalculator()

    @staticmethod
    def _risk(used_margin: Decimal, equity: Decimal) -> Decimal:
        if equity <= 0:
            return Decimal("0.00000000")
        return (used_margin / equity).quantize(
            RISK_QUANT,
            rounding=ROUND_HALF_UP,
        )

    def _calculate_locked_position(
        self,
        position,
        *,
        details,
        instrument,
        latest: dict[str, str],
    ) -> PositionPnlResult | None:
        if position.total_volume <= 0:
            return PositionPnlResult(
                cumulative_unrealized_pnl=Decimal("0.000000"),
                daily_position_pnl=Decimal("0.000000"),
            )
        if (
            instrument is None
            or not latest
            or latest.get("source") != "YML_FEEDHUB"
            or latest.get("ingest_type") != "LIVE_CALLBACK"
            or latest.get("last_price") in (None, "")
        ):
            return None
        mark_price = Decimal(latest["last_price"])
        if mark_price <= 0:
            return None
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
            details=tuple(
                PnlDetailSnapshot(
                    position_detail_id=item.position_detail_id,
                    open_price=Decimal(item.open_price),
                    pnl_base_price=Decimal(item.pnl_base_price),
                    remaining_volume=item.remaining_volume,
                )
                for item in details
                if item.remaining_volume > 0
            ),
        )
        return self.calculator.calculate_position(
            mark_price=mark_price,
            snapshot=snapshot,
        )

    def persist_batch(self, batch_size: int) -> PnlPersistenceResult:
        dirty = self.pnl_store.list_dirty_positions(batch_size)
        if not dirty:
            return PnlPersistenceResult()
        versions = dict(dirty)
        with self.session_factory() as db:
            mappings = (
                self.position_repository.list_account_ids_for_positions(
                    db,
                    list(versions),
                )
            )

        by_account: dict[str, list[str]] = {}
        found_ids = set()
        for position_id, account_id in mappings:
            by_account.setdefault(account_id, []).append(position_id)
            found_ids.add(position_id)

        persisted_ids: list[str] = []
        accounts_persisted = 0
        for account_id in sorted(by_account):
            position_ids = by_account[account_id]
            try:
                with self.session_factory() as db:
                    account = (
                        self.account_repository
                        .get_by_account_id_for_update(db, account_id)
                    )
                    if account is None:
                        db.rollback()
                        continue

                    # 同一账户的Position和PositionDetail均在一次SQL中按稳定
                    # 顺序锁定；Instrument与行情也按不同合约批量读取。
                    positions = (
                        self.position_repository
                        .list_by_position_ids_for_update(
                            db,
                            account_id=account_id,
                            position_ids=position_ids,
                        )
                    )
                    locked_position_ids = [
                        position.position_id for position in positions
                    ]
                    details = (
                        self.position_repository
                        .list_open_details_by_position_ids_for_update(
                            db,
                            position_ids=locked_position_ids,
                        )
                    )
                    details_by_position: dict[str, list] = {}
                    for detail in details:
                        details_by_position.setdefault(
                            detail.position_id,
                            [],
                        ).append(detail)

                    instruments = (
                        self.instrument_repository.list_by_order_book_ids(
                            db,
                            {
                                position.order_book_id
                                for position in positions
                            },
                        )
                    )
                    instrument_by_order_book_id = {
                        instrument.order_book_id: instrument
                        for instrument in instruments
                    }
                    latest_by_contract = (
                        self.market_tick_store.get_latest_many(
                            {
                                (
                                    position.exchange_id,
                                    position.symbol,
                                )
                                for position in positions
                            }
                        )
                    )

                    cumulative_delta = Decimal("0")
                    daily_delta = Decimal("0")
                    updated_positions = []
                    for position in positions:
                        result = self._calculate_locked_position(
                            position,
                            details=details_by_position.get(
                                position.position_id,
                                (),
                            ),
                            instrument=(
                                instrument_by_order_book_id.get(
                                    position.order_book_id
                                )
                            ),
                            latest=latest_by_contract.get(
                                (
                                    position.exchange_id.strip().upper(),
                                    position.symbol.strip().upper(),
                                ),
                                {},
                            ),
                        )
                        if result is None:
                            continue
                        cumulative_delta += (
                            result.cumulative_unrealized_pnl
                            - position.unrealized_pnl
                        )
                        daily_delta += (
                            result.daily_position_pnl
                            - position.daily_position_pnl
                        )
                        position.unrealized_pnl = (
                            result.cumulative_unrealized_pnl
                        )
                        position.daily_position_pnl = (
                            result.daily_position_pnl
                        )
                        position.updated_at = utc_now()
                        updated_positions.append(position_id)

                    if not updated_positions:
                        db.rollback()
                        continue
                    account.unrealized_pnl = quantize_money(
                        account.unrealized_pnl + cumulative_delta
                    )
                    account.daily_position_pnl = quantize_money(
                        account.daily_position_pnl + daily_delta
                    )
                    account.daily_pnl = quantize_money(
                        account.daily_position_pnl
                        + account.daily_close_pnl
                        - account.daily_commission
                    )
                    # 日终尚未实现，累计浮盈仍未进入cash_balance。
                    account.equity = quantize_money(
                        account.cash_balance
                        + account.unrealized_pnl
                    )
                    account.available_cash = quantize_money(
                        account.equity
                        - account.used_margin
                        - account.frozen_margin
                        - account.frozen_cash
                        - account.frozen_commission
                    )
                    account.risk_ratio = self._risk(
                        account.used_margin,
                        account.equity,
                    )
                    account.updated_at = utc_now()
                    db.commit()
                    persisted_ids.extend(updated_positions)
                    accounts_persisted += 1
                self.pnl_store.complete_dirty_account(account_id)
            except Exception:
                # Session上下文会回滚，Dirty版本不删除，下一轮继续重试。
                continue

        # PostgreSQL中已不存在的测试脏标记可以安全清理；正常业务不会删除
        # Position，因此这只防止历史测试数据让集合永久膨胀。
        persisted_ids.extend(set(versions) - found_ids)
        completed = 0
        for position_id in persisted_ids:
            completed += self.pnl_store.complete_dirty_position(
                position_id,
                versions[position_id],
            )
        return PnlPersistenceResult(
            requested=len(dirty),
            positions_persisted=completed,
            accounts_persisted=accounts_persisted,
            retained=len(dirty) - completed,
        )
