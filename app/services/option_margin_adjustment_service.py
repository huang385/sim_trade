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
from app.repositories.position_repository import PositionRepository
from app.services.account_valuation_calculator import AccountValuationCalculator
from app.services.commodity_option_margin_calculator import (
    CommodityFuturesOptionMarginCalculator,
)
from app.services.option_margin_calculator import (
    OptionMarginInput,
    OptionMarginRuleSnapshot,
)


class OptionMarginAdjustmentService:
    """
    对商品期权空头执行保守的账面保证金追加。

    该服务是资金事实的唯一调整入口之一。实时估值 Worker 只能生成派生
    快照；当实时所需保证金高于账面占用时，由本服务按
    Account→Position→PositionDetail 的固定顺序加锁、重算并提交。
    行情下降不会自动释放账面保证金，释放仍由平仓或后续结算完成。
    """

    def __init__(
        self,
        *,
        market_tick_store: MarketTickStore,
        account_repository: AccountRepository | None = None,
        position_repository: PositionRepository | None = None,
        instrument_repository: InstrumentRepository | None = None,
    ):
        self.market_tick_store = market_tick_store
        self.account_repository = account_repository or AccountRepository()
        self.position_repository = position_repository or PositionRepository()
        self.instrument_repository = (
            instrument_repository or InstrumentRepository()
        )

    @staticmethod
    def _rule(position) -> tuple[OptionMarginRuleSnapshot, Decimal, Decimal]:
        values = position.margin_rule_snapshot or {}
        try:
            return (
                OptionMarginRuleSnapshot(
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
                ),
                Decimal(values["underlying_margin_rate"]),
                Decimal(values["underlying_multiplier"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DataAccessError(
                "期权持仓保证金规则快照不完整",
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
                or position.instrument_type
                != InstrumentType.FUTURES_OPTION.value
                or position.direction != "SHORT"
            ):
                raise DataAccessError(
                    "目标不是有效的商品期权空头持仓",
                    error_code="OPTION_MARGIN_POSITION_INVALID",
                )
            details = self.position_repository.list_open_details_for_update(
                db, position_id=position.position_id
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
            rule, underlying_rate, underlying_multiplier = self._rule(position)
            required = CommodityFuturesOptionMarginCalculator().calculate(
                OptionMarginInput(
                    option_type=OptionType(instrument.option_type),
                    strike_price=Decimal(instrument.strike_price),
                    option_price=option_tick.last_price,
                    underlying_price=underlying_tick.last_price,
                    option_multiplier=Decimal(
                        instrument.contract_multiplier
                    ),
                    underlying_multiplier=underlying_multiplier,
                    volume=position.total_volume,
                    price_mode=MarginPriceMode.SETTLEMENT,
                    calculated_at=utc_now(),
                    rule=rule,
                    underlying_margin_per_lot=quantize_money(
                        underlying_tick.last_price
                        * underlying_multiplier
                        * underlying_rate
                    ),
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
            additional = max(
                quantize_money(required - position.used_margin),
                Decimal("0"),
            )
            active_details = [
                item for item in details if item.remaining_volume > 0
            ]
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
            if proposed_valuation.risk_available_cash < Decimal("0"):
                account.available_cash = proposed_valuation.available_cash
                account.risk_available_cash = (
                    proposed_valuation.risk_available_cash
                )
                account.equity = proposed_valuation.equity
                account.net_option_market_value = (
                    proposed_valuation.net_option_market_value
                )
                account.risk_state = AccountRiskState.MARGIN_DEFICIT.value
                position.margin_price_mode = MarginPriceMode.SETTLEMENT.value
                position.margin_underlying_price = underlying_tick.last_price
                position.margin_option_price = option_tick.last_price
                position.margin_calculated_at = utc_now()
                account.updated_at = utc_now()
                position.updated_at = utc_now()
                db.commit()
                return account
            if additional > 0:
                position.used_margin = quantize_money(
                    position.used_margin + additional
                )
                account.used_margin = quantize_money(
                    account.used_margin + additional
                )
                account.option_used_margin = quantize_money(
                    account.option_used_margin + additional
                )
                remaining_addition = additional
                for index, detail in enumerate(active_details):
                    share = (
                        remaining_addition
                        if index == len(active_details) - 1
                        else quantize_money(
                            additional
                            * Decimal(detail.remaining_volume)
                            / Decimal(total_volume)
                        )
                    )
                    detail.remaining_margin = quantize_money(
                        detail.remaining_margin + share
                    )
                    detail.realtime_required_margin = quantize_money(
                        detail.realtime_required_margin + share
                    )
                    remaining_addition = quantize_money(
                        remaining_addition - share
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
                AccountRiskState.MARGIN_DEFICIT.value
                if valuation.risk_available_cash < 0
                else AccountRiskState.NORMAL.value
            )
            position.margin_price_mode = MarginPriceMode.SETTLEMENT.value
            position.margin_underlying_price = underlying_tick.last_price
            position.margin_option_price = option_tick.last_price
            position.margin_calculated_at = utc_now()
            account.updated_at = utc_now()
            position.updated_at = utc_now()
            db.commit()
            return account
        except Exception:
            db.rollback()
            raise
