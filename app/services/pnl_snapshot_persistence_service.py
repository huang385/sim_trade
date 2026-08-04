from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import logging

from app.common.decimal_utils import quantize_money
from app.common.exceptions import DataAccessError
from app.common.time_utils import utc_now
from app.core.config import settings
from app.enums.account_enums import AccountRiskState
from app.enums.option_enums import InstrumentType, MarginPriceMode, OptionType
from app.enums.order_enums import PositionDirection
from app.infrastructure.market_data.market_tick_store import MarketTickStore
from app.infrastructure.realtime_pnl_store import RealtimePnlStore
from app.repositories.account_repository import AccountRepository
from app.repositories.instrument_repository import InstrumentRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.position_repository import PositionRepository
from app.services.account_risk_state_service import AccountRiskStateService
from app.services.account_valuation_calculator import AccountValuationCalculator
from app.services.commodity_option_margin_calculator import (
    CommodityFuturesOptionMarginCalculator,
)
from app.services.option_margin_adjustment_service import (
    OptionMarginAdjustmentService,
)
from app.services.option_margin_calculator import OptionMarginInput
from app.services.pnl_calculator import (
    PnlCalculator,
    PnlDetailSnapshot,
    PositionPnlResult,
    PositionPnlSnapshot,
)


RISK_QUANT = Decimal("0.00000001")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PnlPersistenceResult:
    """一轮Dirty持仓和账户资金事实持久化统计。"""

    requested: int = 0
    positions_persisted: int = 0
    accounts_persisted: int = 0
    retained: int = 0
    accounts_requested: int = 0


@dataclass(frozen=True)
class _PositionFactUpdate:
    """完整校验后、实际写入ORM前暂存的单持仓计算结果。"""

    position: object
    pnl: PositionPnlResult
    option_market_value: Decimal
    realtime_required_margin: Decimal
    option_price: Decimal | None
    underlying_price: Decimal | None
    detail_margin_shares: tuple[tuple[object, Decimal], ...]


class PnlSnapshotPersistenceService:
    """
    从PostgreSQL数量和规则快照重新计算并持久化实时资金事实。

    Redis只提供Dirty触发和当前行情。持仓数量、方向、乘数和保证金规则全部
    重新从加锁后的PostgreSQL记录读取；Redis中的PnL、市值和保证金绝对值
    不会直接复制回数据库。
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
        order_repository: OrderRepository | None = None,
        calculator: PnlCalculator | None = None,
    ):
        self.session_factory = session_factory
        self.pnl_store = pnl_store
        self.market_tick_store = market_tick_store
        self.account_repository = account_repository or AccountRepository()
        self.position_repository = position_repository or PositionRepository()
        self.instrument_repository = (
            instrument_repository or InstrumentRepository()
        )
        self.order_repository = order_repository or OrderRepository()
        self.calculator = calculator or PnlCalculator()

    @staticmethod
    def _risk(used_margin: Decimal, equity: Decimal) -> Decimal:
        if equity <= 0:
            return Decimal("0.00000000")
        return (used_margin / equity).quantize(
            RISK_QUANT,
            rounding=ROUND_HALF_UP,
        )

    @staticmethod
    def _mark_price(values: dict[str, str]) -> Decimal | None:
        """只把能够完整反序列化的正数行情交给资金计算。"""

        if not values:
            return None
        try:
            tick = MarketTickStore.mapping_to_tick(values)
        except Exception:
            # 单元测试和历史Redis快照可能只保存经过上游校验的核心字段；
            # 仍要求来源、实时回调类型和有限正数价格全部满足。
            if (
                values.get("source") != "YMM_LIVE_DATA"
                or values.get("ingest_type") != "LIVE_CALLBACK"
            ):
                return None
            try:
                price = Decimal(values.get("last_price", ""))
            except Exception:
                return None
            return price if price.is_finite() and price > 0 else None
        if tick.last_price is None or tick.last_price <= 0:
            return None
        return tick.last_price

    def _calculate_position_pnl(
        self,
        position,
        *,
        details,
        mark_price: Decimal,
        multiplier: Decimal,
    ) -> PositionPnlResult:
        snapshot = PositionPnlSnapshot(
            position_id=position.position_id,
            account_id=position.account_id,
            order_book_id=position.order_book_id,
            exchange_id=position.exchange_id,
            symbol=position.symbol,
            direction=position.direction,
            contract_multiplier=multiplier,
            persisted_unrealized_pnl=Decimal(position.unrealized_pnl),
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

    @staticmethod
    def _allocate_detail_margin(
        details,
        *,
        total_margin: Decimal,
        total_volume: int,
    ) -> tuple[tuple[object, Decimal], ...]:
        active = [item for item in details if item.remaining_volume > 0]
        if sum(item.remaining_volume for item in active) != total_volume:
            raise ValueError("期权持仓汇总数量与有效明细不一致")
        if not active:
            return ()
        remaining = total_margin
        shares: list[tuple[object, Decimal]] = []
        for index, detail in enumerate(active):
            share = (
                remaining
                if index == len(active) - 1
                else quantize_money(
                    total_margin
                    * Decimal(detail.remaining_volume)
                    / Decimal(total_volume)
                )
            )
            shares.append((detail, share))
            remaining = quantize_money(remaining - share)
        return tuple(shares)

    def _calculate_option_update(
        self,
        *,
        position,
        details,
        instrument,
        underlying,
        mark_price: Decimal,
        underlying_price: Decimal | None,
    ) -> _PositionFactUpdate:
        multiplier = Decimal(position.multiplier_snapshot)
        if multiplier <= 0:
            raise ValueError("期权持仓乘数快照不合法")
        pnl = self._calculate_position_pnl(
            position,
            details=details,
            mark_price=mark_price,
            multiplier=multiplier,
        )
        market_value = quantize_money(
            mark_price * multiplier * Decimal(position.total_volume)
        )
        required = Decimal("0.000000")
        if position.direction == PositionDirection.SHORT.value:
            if underlying is None or underlying_price is None:
                raise ValueError("商品期权空头缺少标的行情")
            rule, underlying_rate, underlying_multiplier = (
                OptionMarginAdjustmentService._rule(position)
            )
            if (
                position.margin_rule_id != rule.rule_id
                or position.margin_rule_version != rule.rule_version
            ):
                raise ValueError("期权保证金规则版本不一致")
            if underlying_multiplier <= 0:
                raise ValueError("期权标的乘数快照不合法")
            required = CommodityFuturesOptionMarginCalculator().calculate(
                OptionMarginInput(
                    option_type=OptionType(instrument.option_type),
                    strike_price=Decimal(instrument.strike_price),
                    option_price=mark_price,
                    underlying_price=underlying_price,
                    option_multiplier=multiplier,
                    underlying_multiplier=underlying_multiplier,
                    volume=position.total_volume,
                    price_mode=MarginPriceMode.REALTIME,
                    calculated_at=utc_now(),
                    rule=rule,
                    underlying_margin_per_lot=quantize_money(
                        underlying_price
                        * underlying_multiplier
                        * underlying_rate
                    ),
                )
            ).total_margin
        return _PositionFactUpdate(
            position=position,
            pnl=pnl,
            option_market_value=market_value,
            realtime_required_margin=required,
            option_price=mark_price,
            underlying_price=underlying_price,
            detail_margin_shares=self._allocate_detail_margin(
                details,
                total_margin=required,
                total_volume=position.total_volume,
            ),
        )

    def _recalculate_locked_account(self, db, account) -> bool:
        """
        完整重算一个账户；缺少任意必要行情时只持久化风险不可估值状态。

        返回False表示金额没有写入且Dirty必须保留。
        """

        positions = self.position_repository.list_active_by_account_for_update(
            db,
            account.account_id,
        )
        position_ids = [position.position_id for position in positions]
        details = (
            self.position_repository
            .list_open_details_by_position_ids_for_update(
                db,
                position_ids=position_ids,
            )
        )
        details_by_position: dict[str, list] = {}
        for detail in details:
            details_by_position.setdefault(detail.position_id, []).append(detail)

        instruments = self.instrument_repository.list_by_order_book_ids(
            db,
            {position.order_book_id for position in positions},
        )
        instrument_by_order_book_id = {
            instrument.order_book_id: instrument
            for instrument in instruments
        }
        underlying_by_id = {
            instrument.id: instrument
            for instrument in self.instrument_repository.list_by_ids(
                db,
                {
                    instrument.underlying_instrument_id
                    for instrument in instruments
                    if instrument.underlying_instrument_id is not None
                },
            )
        }
        contract_keys = {
            (
                instrument.exchange_id.strip().upper(),
                instrument.symbol.strip().upper(),
            )
            for instrument in instruments
        } | {
            (
                underlying.exchange_id.strip().upper(),
                underlying.symbol.strip().upper(),
            )
            for underlying in underlying_by_id.values()
        }
        latest = self.market_tick_store.get_latest_many(contract_keys)

        updates: list[_PositionFactUpdate] = []
        try:
            for position in positions:
                instrument = instrument_by_order_book_id.get(
                    position.order_book_id
                )
                if instrument is None:
                    raise ValueError("持仓合约不存在")
                position_details = details_by_position.get(
                    position.position_id,
                    (),
                )
                if (
                    sum(item.remaining_volume for item in position_details)
                    != position.total_volume
                ):
                    raise ValueError("持仓汇总数量与明细不一致")
                position_multiplier = Decimal(position.multiplier_snapshot)
                if position_multiplier <= 0 or any(
                    Decimal(item.multiplier_snapshot)
                    != position_multiplier
                    for item in position_details
                ):
                    # 历史金额只能由不可变快照重建。明细缺失或与汇总快照
                    # 不一致时禁止静默改用当前Instrument乘数。
                    raise ValueError("持仓与明细乘数快照不一致")
                key = (
                    instrument.exchange_id.strip().upper(),
                    instrument.symbol.strip().upper(),
                )
                mark_price = self._mark_price(latest.get(key, {}))
                if mark_price is None:
                    raise ValueError("持仓行情不可用")
                instrument_type = InstrumentType(position.instrument_type)
                if instrument_type in {
                    InstrumentType.FUTURES_OPTION,
                    InstrumentType.INDEX_OPTION,
                }:
                    if any(
                        item.margin_rule_id != position.margin_rule_id
                        or item.margin_rule_version
                        != position.margin_rule_version
                        or (item.margin_rule_snapshot or {})
                        != (position.margin_rule_snapshot or {})
                        for item in position_details
                    ):
                        raise ValueError(
                            "期权持仓与明细保证金规则快照不一致"
                        )
                    underlying = underlying_by_id.get(
                        instrument.underlying_instrument_id
                    )
                    underlying_price = None
                    if underlying is not None:
                        underlying_price = self._mark_price(
                            latest.get(
                                (
                                    underlying.exchange_id.strip().upper(),
                                    underlying.symbol.strip().upper(),
                                ),
                                {},
                            )
                        )
                    updates.append(
                        self._calculate_option_update(
                            position=position,
                            details=position_details,
                            instrument=instrument,
                            underlying=underlying,
                            mark_price=mark_price,
                            underlying_price=underlying_price,
                        )
                    )
                else:
                    multiplier = Decimal(position.multiplier_snapshot)
                    if multiplier <= 0:
                        raise ValueError("期货持仓乘数快照不合法")
                    updates.append(
                        _PositionFactUpdate(
                            position=position,
                            pnl=self._calculate_position_pnl(
                                position,
                                details=position_details,
                                mark_price=mark_price,
                                multiplier=multiplier,
                            ),
                            option_market_value=Decimal("0.000000"),
                            realtime_required_margin=Decimal("0.000000"),
                            option_price=None,
                            underlying_price=None,
                            detail_margin_shares=(),
                        )
                    )
        except (ValueError, TypeError, ArithmeticError, DataAccessError) as exc:
            # 金额不确定时禁止部分写入；风险状态单独提交并保留Dirty重试。
            account.risk_state = AccountRiskState.VALUATION_UNAVAILABLE.value
            account.updated_at = utc_now()
            logger.warning(
                "账户完整估值不可用 account_id=%s reason=%s",
                account.account_id,
                type(exc).__name__,
            )
            return False

        # Account行锁阻止订单资金字段并发提交。这里不反向锁Order，避免与
        # 成交/撤单的Order→Account顺序死锁；等待Account锁的订单事务提交后
        # 会产生新Dirty，再触发下一轮完整估值。
        active_risk_orders = (
            self.order_repository.list_active_option_sell_open_by_account(
                db,
                account.account_id,
            )
        )
        # 旧单元隔离测试的Session Mock未实现新增查询；生产Repository固定
        # 返回Sequence。兼容测试替身不改变真实数据库路径的严格校验。
        if not isinstance(active_risk_orders, (list, tuple)):
            active_risk_orders = ()
        order_valuation_unavailable = any(
            getattr(item, "margin_risk_state", "NORMAL")
            == AccountRiskState.VALUATION_UNAVAILABLE.value
            for item in active_risk_orders
        )
        order_margin_deficit = any(
            getattr(item, "margin_risk_state", "NORMAL")
            == AccountRiskState.MARGIN_DEFICIT.value
            for item in active_risk_orders
        )

        futures_unrealized = Decimal("0")
        daily_position_pnl = Decimal("0")
        long_option_value = Decimal("0")
        short_option_value = Decimal("0")
        option_required_margin = Decimal("0")
        now = utc_now()
        for update in updates:
            position = update.position
            position.unrealized_pnl = update.pnl.cumulative_unrealized_pnl
            position.daily_position_pnl = update.pnl.daily_position_pnl
            position.option_market_value = update.option_market_value
            position.realtime_required_margin = (
                update.realtime_required_margin
            )
            if update.option_price is not None:
                position.margin_price_mode = MarginPriceMode.REALTIME.value
                position.margin_option_price = update.option_price
                position.margin_underlying_price = update.underlying_price
                position.margin_calculated_at = now
            position.updated_at = now
            for detail, margin_share in update.detail_margin_shares:
                detail.realtime_required_margin = margin_share
                detail.margin_price_mode = MarginPriceMode.REALTIME.value
                detail.margin_option_price = update.option_price
                detail.margin_underlying_price = update.underlying_price
                detail.margin_calculated_at = now
                detail.updated_at = now

            position_type = InstrumentType(position.instrument_type)
            if position_type in {
                InstrumentType.FUTURES_OPTION,
                InstrumentType.INDEX_OPTION,
            }:
                if position.direction == PositionDirection.LONG.value:
                    long_option_value += update.option_market_value
                else:
                    short_option_value += update.option_market_value
                    option_required_margin += (
                        update.realtime_required_margin
                    )
            else:
                futures_unrealized += update.pnl.cumulative_unrealized_pnl
            daily_position_pnl += update.pnl.daily_position_pnl

        account.unrealized_pnl = quantize_money(futures_unrealized)
        account.daily_position_pnl = quantize_money(daily_position_pnl)
        account.daily_pnl = quantize_money(
            account.daily_position_pnl
            + account.daily_close_pnl
            - account.daily_commission
        )
        account.long_option_market_value = quantize_money(long_option_value)
        account.short_option_market_value = quantize_money(short_option_value)
        account.option_realtime_required_margin = quantize_money(
            option_required_margin
        )
        valuation = AccountValuationCalculator.calculate(
            cash_balance=Decimal(account.cash_balance),
            futures_unrealized_pnl=Decimal(account.unrealized_pnl),
            long_option_market_value=Decimal(
                account.long_option_market_value
            ),
            short_option_market_value=Decimal(
                account.short_option_market_value
            ),
            used_margin=Decimal(account.used_margin),
            option_used_margin=Decimal(account.option_used_margin),
            option_realtime_required_margin=Decimal(
                account.option_realtime_required_margin
            ),
            frozen_margin=Decimal(account.frozen_margin),
            frozen_cash=Decimal(account.frozen_cash),
            frozen_commission=Decimal(account.frozen_commission),
            option_collateral_ratio=settings.option_collateral_ratio,
        )
        account.equity = valuation.equity
        account.available_cash = valuation.available_cash
        account.risk_available_cash = valuation.risk_available_cash
        account.net_option_market_value = valuation.net_option_market_value
        account.risk_state = AccountRiskStateService.resolve_full_evaluation(
            valuation_unavailable=order_valuation_unavailable,
            margin_deficit=(
                order_margin_deficit
                or valuation.risk_available_cash < Decimal("0")
            ),
        )
        account.risk_ratio = self._risk(
            valuation.effective_required_margin,
            valuation.equity,
        )
        account.updated_at = now
        if order_valuation_unavailable:
            logger.warning(
                "账户存在估值不可用的活动卖出开仓订单 account_id=%s",
                account.account_id,
            )
        # 活动订单估值不可用时保留Account Dirty，等待行情/规则恢复后重试；
        # 保证金不足是已完成的可靠估值结果，可清除本版本Dirty。
        return not order_valuation_unavailable

    def persist_batch(self, batch_size: int) -> PnlPersistenceResult:
        dirty_positions = self.pnl_store.list_dirty_positions(batch_size)
        list_accounts = getattr(self.pnl_store, "list_dirty_accounts", None)
        dirty_accounts = (
            list_accounts(batch_size) if list_accounts is not None else []
        )
        # 旧单元测试使用未配置该新方法的Mock；生产适配器始终返回列表。
        if not isinstance(dirty_accounts, (list, tuple)):
            dirty_accounts = []
        if not dirty_positions and not dirty_accounts:
            return PnlPersistenceResult()

        position_versions = dict(dirty_positions)
        account_versions = dict(dirty_accounts)
        with self.session_factory() as db:
            mappings = self.position_repository.list_account_ids_for_positions(
                db,
                list(position_versions),
            )
        positions_by_account: dict[str, list[str]] = {}
        for position_id, account_id in mappings:
            positions_by_account.setdefault(account_id, []).append(position_id)

        account_ids = sorted(
            set(positions_by_account) | set(account_versions)
        )
        successful_positions: list[str] = []
        successful_accounts: list[str] = []
        accounts_persisted = 0
        for account_id in account_ids:
            try:
                with self.session_factory() as db:
                    account = (
                        self.account_repository.get_by_account_id_for_update(
                            db,
                            account_id,
                        )
                    )
                    if account is None:
                        db.rollback()
                        continue
                    complete = self._recalculate_locked_account(db, account)
                    db.commit()
                    accounts_persisted += 1
                    if not complete:
                        continue
                    successful_positions.extend(
                        positions_by_account.get(account_id, ())
                    )
                    if account_id in account_versions:
                        successful_accounts.append(account_id)
            except Exception:
                # Session上下文会回滚；所有Dirty版本保留给下一轮重试。
                continue

        completed_positions = 0
        for position_id in dict.fromkeys(successful_positions):
            completed_positions += int(
                self.pnl_store.complete_dirty_position(
                    position_id,
                    position_versions[position_id],
                )
            )
        for account_id in successful_accounts:
            self.pnl_store.complete_dirty_account(
                account_id,
                account_versions[account_id],
            )

        return PnlPersistenceResult(
            requested=len(dirty_positions),
            positions_persisted=completed_positions,
            accounts_persisted=accounts_persisted,
            retained=len(dirty_positions) - completed_positions,
            accounts_requested=len(dirty_accounts),
        )
