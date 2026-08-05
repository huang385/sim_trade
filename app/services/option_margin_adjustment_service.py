from decimal import Decimal

from sqlalchemy.orm import Session

from app.common.decimal_utils import quantize_money
from app.common.exceptions import DataAccessError
from app.common.time_utils import utc_now
from app.core.config import settings
from app.enums.account_enums import AccountRiskState
from app.enums.option_enums import (
    InstrumentType,
    MarginPriceMode,
    OptionType,
)
from app.infrastructure.market_data.market_tick_store import MarketTickStore
from app.repositories.account_repository import AccountRepository
from app.repositories.instrument_repository import InstrumentRepository
from app.repositories.outbox_repository import OutboxRepository
from app.repositories.position_repository import PositionRepository
from app.services.account_valuation_calculator import AccountValuationCalculator
from app.services.account_risk_state_service import AccountRiskStateService
from app.services.option_margin_calculator import (
    OptionMarginInput,
    OptionMarginRuleSnapshot,
)
from app.services.option_margin_calculator_resolver import (
    OptionMarginCalculatorResolver,
)
from app.services.realtime_fact_event_service import RealtimeFactEventService


class OptionMarginAdjustmentService:
    """
    按500ms合并后的最新行情双向调整期权空头账面保证金。

    该服务是资金事实的唯一调整入口之一。实时估值 Worker 只能生成派生
    快照；当实时所需保证金与账面占用不一致时，由本服务按
    Account→Position→PositionDetail 的固定顺序加锁、重算并提交。上涨时
    追加、下降时释放，不设置金额阈值；initial_occupied_margin始终保留
    成交时的历史值。
    """

    def __init__(
        self,
        *,
        market_tick_store: MarketTickStore,
        account_repository: AccountRepository | None = None,
        position_repository: PositionRepository | None = None,
        instrument_repository: InstrumentRepository | None = None,
        option_margin_resolver: OptionMarginCalculatorResolver | None = None,
        realtime_fact_events: RealtimeFactEventService | None = None,
    ):
        self.market_tick_store = market_tick_store
        self.account_repository = account_repository or AccountRepository()
        self.position_repository = position_repository or PositionRepository()
        self.instrument_repository = (
            instrument_repository or InstrumentRepository()
        )
        self.option_margin_resolver = (
            option_margin_resolver or OptionMarginCalculatorResolver()
        )
        self.realtime_fact_events = (
            realtime_fact_events
            or RealtimeFactEventService(repository=OutboxRepository())
        )

    @staticmethod
    def _rule(position) -> OptionMarginRuleSnapshot:
        values = position.margin_rule_snapshot or {}
        try:
            return OptionMarginRuleSnapshot(
                rule_id=int(values["rule_id"]),
                rule_version=str(values["rule_version"]),
                margin_algorithm=str(values["margin_algorithm"]),
                margin_adjustment_rate=Decimal(
                    values["margin_adjustment_rate"]
                ),
                minimum_guarantee_rate=Decimal(
                    values["minimum_guarantee_rate"]
                ),
                out_of_money_deduction_rate=Decimal(
                    values["out_of_money_deduction_rate"]
                ),
                minimum_underlying_margin_ratio=Decimal(
                    values["minimum_underlying_margin_ratio"]
                ),
                extra_margin_rate=Decimal(values["extra_margin_rate"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DataAccessError(
                "期权持仓保证金规则快照不完整",
                error_code="OPTION_MARGIN_SNAPSHOT_INCOMPLETE",
            ) from exc

    @staticmethod
    def _commodity_underlying_inputs(position) -> tuple[Decimal, Decimal]:
        """读取商品期权专用快照；股指期权不会调用本方法。"""

        values = position.margin_rule_snapshot or {}
        try:
            return (
                Decimal(values["underlying_margin_rate"]),
                Decimal(values["underlying_multiplier"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DataAccessError(
                "商品期权标的保证金快照不完整",
                error_code="OPTION_MARGIN_SNAPSHOT_INCOMPLETE",
            ) from exc

    def adjust(self, db: Session, *, account_id: str, position_id: str):
        try:
            account = self.account_repository.get_by_account_id_for_update(
                db, account_id
            )
            if account is None:
                raise DataAccessError(
                    "保证金调整账户不存在",
                    error_code="ACCOUNT_NOT_FOUND",
                )
            position = self.position_repository.get_by_position_id_for_update(
                db, position_id
            )
            if (
                position is None
                or position.account_id != account_id
                or position.instrument_type not in {
                    InstrumentType.FUTURES_OPTION.value,
                    InstrumentType.INDEX_OPTION.value,
                }
                or position.direction != "SHORT"
            ):
                raise DataAccessError(
                    "目标不是有效的期权空头持仓",
                    error_code="OPTION_MARGIN_POSITION_INVALID",
                )
            details = self.position_repository.list_open_details_for_update(
                db, position_id=position.position_id
            )
            position_multiplier = Decimal(position.multiplier_snapshot)
            if position_multiplier <= 0 or any(
                Decimal(item.multiplier_snapshot) != position_multiplier
                for item in details
                if item.remaining_volume > 0
            ):
                raise DataAccessError(
                    "期权持仓与明细乘数快照不一致",
                    error_code="OPTION_MARGIN_MULTIPLIER_INCONSISTENT",
                )
            instrument = self.instrument_repository.get_by_order_book_id(
                db, position.order_book_id
            )
            underlying = (
                self.instrument_repository.get_by_id(
                    db, instrument.underlying_instrument_id
                )
                if instrument is not None
                and instrument.underlying_instrument_id is not None
                else None
            )
            if instrument is None or underlying is None:
                raise DataAccessError(
                    "期权或标的合约不存在",
                    error_code="OPTION_UNDERLYING_NOT_FOUND",
                )
            option_key = (
                instrument.exchange_id.strip().upper(),
                instrument.symbol.strip().upper(),
            )
            underlying_key = (
                underlying.exchange_id.strip().upper(),
                underlying.symbol.strip().upper(),
            )
            latest = self.market_tick_store.get_latest_many(
                {
                    option_key,
                    underlying_key,
                }
            )
            option_values = latest.get(option_key, {})
            underlying_values = latest.get(underlying_key, {})
            if not option_values or not underlying_values:
                raise DataAccessError(
                    "期权保证金调整缺少期权或标的最新行情",
                    error_code="OPTION_MARGIN_PRICE_UNAVAILABLE",
                )
            try:
                option_tick = MarketTickStore.mapping_to_tick(option_values)
                underlying_tick = MarketTickStore.mapping_to_tick(
                    underlying_values
                )
            except Exception as exc:
                raise DataAccessError(
                    "期权保证金调整行情格式不合法",
                    error_code="OPTION_MARGIN_PRICE_UNAVAILABLE",
                ) from exc
            if (
                option_tick.last_price is None
                or option_tick.last_price <= 0
                or underlying_tick.last_price is None
                or underlying_tick.last_price <= 0
            ):
                raise DataAccessError(
                    "期权保证金调整缺少有效行情",
                    error_code="OPTION_MARGIN_PRICE_UNAVAILABLE",
                )
            rule = self._rule(position)
            underlying_multiplier = Decimal("1")
            underlying_margin_per_lot = Decimal("0.000000")
            if position.instrument_type == InstrumentType.FUTURES_OPTION.value:
                underlying_rate, underlying_multiplier = (
                    self._commodity_underlying_inputs(position)
                )
                underlying_margin_per_lot = quantize_money(
                    underlying_tick.last_price
                    * underlying_multiplier
                    * underlying_rate
                )
            else:
                underlying_multiplier = Decimal(
                    underlying.contract_multiplier
                )
            calculator = self.option_margin_resolver.resolve(
                instrument_type=position.instrument_type,
                exchange_id=instrument.exchange_id,
                margin_algorithm=rule.margin_algorithm,
            )
            required = calculator.calculate(
                OptionMarginInput(
                    option_type=OptionType(instrument.option_type),
                    strike_price=Decimal(instrument.strike_price),
                    option_price=option_tick.last_price,
                    underlying_price=underlying_tick.last_price,
                    option_multiplier=position_multiplier,
                    underlying_multiplier=underlying_multiplier,
                    volume=position.total_volume,
                    price_mode=MarginPriceMode.SETTLEMENT,
                    calculated_at=utc_now(),
                    rule=rule,
                    underlying_margin_per_lot=underlying_margin_per_lot,
                )
            ).total_margin
            previous_required = Decimal(
                position.realtime_required_margin or 0
            )
            position.realtime_required_margin = required
            account.option_realtime_required_margin = quantize_money(
                account.option_realtime_required_margin
                - previous_required
                + required
            )
            margin_delta = quantize_money(
                required - Decimal(position.used_margin)
            )
            active_details = [
                item for item in details if item.remaining_volume > 0
            ]
            if any(
                item.margin_rule_id != position.margin_rule_id
                or item.margin_rule_version != position.margin_rule_version
                or (item.margin_rule_snapshot or {})
                != (position.margin_rule_snapshot or {})
                for item in active_details
            ):
                raise DataAccessError(
                    "期权持仓与明细保证金规则快照不一致",
                    error_code="OPTION_MARGIN_RULE_INCONSISTENT",
                )
            total_volume = sum(
                item.remaining_volume for item in active_details
            )
            if total_volume != position.total_volume:
                raise DataAccessError(
                    "期权持仓汇总数量与有效明细不一致",
                    error_code="OPTION_MARGIN_POSITION_INCONSISTENT",
                )

            # 不能直接使用数据库中上一次持久化的 risk_available_cash。
            # 该字段可能落后于最新行情；必须先把本次实时所需保证金代入统一
            # 估值公式，再根据新的风险可用资金决定是否允许追加账面保证金。
            proposed_valuation = AccountValuationCalculator.calculate(
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
            # 只有向上追加才受资金是否充足约束。向下释放会改善账户风险，
            # 即使账户已经处于MARGIN_DEFICIT也必须允许继续执行。
            if (
                margin_delta > 0
                and proposed_valuation.risk_available_cash < Decimal("0")
            ):
                remaining_required = required
                for index, detail in enumerate(active_details):
                    share = (
                        remaining_required
                        if index == len(active_details) - 1
                        else quantize_money(
                            required
                            * Decimal(detail.remaining_volume)
                            / Decimal(total_volume)
                        )
                    )
                    # 资金不足时不增加账面remaining_margin，但明细的实时
                    # 风险要求必须反映最新计算结果，避免展示历史旧值。
                    detail.realtime_required_margin = share
                    remaining_required = quantize_money(
                        remaining_required - share
                    )
                account.available_cash = proposed_valuation.available_cash
                account.risk_available_cash = (
                    proposed_valuation.risk_available_cash
                )
                account.equity = proposed_valuation.equity
                account.net_option_market_value = (
                    proposed_valuation.net_option_market_value
                )
                account.risk_state = (
                    AccountRiskStateService.preserve_for_local_update(
                        getattr(
                            account,
                            "risk_state",
                            AccountRiskState.NORMAL.value,
                        ),
                        margin_deficit=True,
                    )
                )
                position.margin_price_mode = MarginPriceMode.SETTLEMENT.value
                position.margin_underlying_price = underlying_tick.last_price
                position.margin_option_price = option_tick.last_price
                adjusted_at = utc_now()
                position.margin_calculated_at = adjusted_at
                account.updated_at = adjusted_at
                position.updated_at = adjusted_at
                # 资金不足时没有增加Account.used_margin，因此不能虚构
                # ACCOUNT_FACT_UPDATED；但持仓实时要求已经变化，仍需通过
                # POSITION_UPDATED可靠触发后续风险重算和页面绝对值更新。
                if required != previous_required:
                    self.realtime_fact_events.create_position_updated(
                        db,
                        position=position,
                        occurred_at=adjusted_at,
                        fact_reason="OPTION_MARGIN_ADJUSTMENT",
                    )
                db.commit()
                return account

            # 不设置阈值：只要六位小数量化后的差额非零，就把账面实际占用
            # 精确同步到本轮加锁后重新计算的required。账户总额只调整当前
            # 持仓的差额，不影响其他期货或期权持仓。
            position.used_margin = required
            account.used_margin = quantize_money(
                Decimal(account.used_margin) + margin_delta
            )
            account.option_used_margin = quantize_money(
                Decimal(account.option_used_margin) + margin_delta
            )
            if account.used_margin < 0 or account.option_used_margin < 0:
                raise DataAccessError(
                    "期权保证金释放后的账户占用金额不合法",
                    error_code="OPTION_MARGIN_ACCOUNT_INCONSISTENT",
                )

            # 按剩余数量重新分配目标金额，而不是在历史值上简单加减。最后
            # 一条明细承接Decimal量化尾差，保证明细合计始终等于Position。
            remaining_required = required
            for index, detail in enumerate(active_details):
                share = (
                    remaining_required
                    if index == len(active_details) - 1
                    else quantize_money(
                        required
                        * Decimal(detail.remaining_volume)
                        / Decimal(total_volume)
                    )
                )
                detail.remaining_margin = share
                detail.realtime_required_margin = share
                remaining_required = quantize_money(
                    remaining_required - share
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
            account.available_cash = valuation.available_cash
            account.risk_available_cash = valuation.risk_available_cash
            account.equity = valuation.equity
            account.risk_state = (
                AccountRiskStateService.preserve_for_local_update(
                    getattr(
                        account,
                        "risk_state",
                        AccountRiskState.NORMAL.value,
                    ),
                    margin_deficit=(
                        valuation.risk_available_cash < Decimal("0")
                    ),
                )
            )
            position.margin_price_mode = MarginPriceMode.SETTLEMENT.value
            position.margin_underlying_price = underlying_tick.last_price
            position.margin_option_price = option_tick.last_price
            adjusted_at = utc_now()
            position.margin_calculated_at = adjusted_at
            account.updated_at = adjusted_at
            position.updated_at = adjusted_at
            # Account、Position和两条事实Outbox共用当前事务。任一Outbox
            # 创建失败都会进入统一rollback，不能留下只改数据库未通知的
            # 半完成保证金状态。
            if margin_delta != Decimal("0"):
                self.realtime_fact_events.create_account_updated(
                    db,
                    account=account,
                    occurred_at=adjusted_at,
                    fact_reason="OPTION_MARGIN_ADJUSTMENT",
                )
            if (
                margin_delta != Decimal("0")
                or required != previous_required
            ):
                self.realtime_fact_events.create_position_updated(
                    db,
                    position=position,
                    occurred_at=adjusted_at,
                    fact_reason="OPTION_MARGIN_ADJUSTMENT",
                )
            db.commit()
            return account
        except Exception:
            db.rollback()
            raise
