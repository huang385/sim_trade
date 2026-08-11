from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from app.common.decimal_utils import quantize_money
from app.common.exceptions import DataAccessError
from app.common.time_utils import utc_now
from app.enums.account_enums import AccountRiskState
from app.enums.option_enums import InstrumentType, MarginPriceMode, OptionType
from app.enums.order_enums import OffsetFlag, OrderDirection, OrderStatus
from app.infrastructure.market_data.market_tick_store import MarketTickStore
from app.repositories.account_repository import AccountRepository
from app.repositories.instrument_repository import InstrumentRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.outbox_repository import OutboxRepository
from app.services.account_risk_state_service import AccountRiskStateService
from app.services.option_margin_adjustment_service import (
    OptionMarginAdjustmentService,
)
from app.services.option_margin_calculator import OptionMarginInput
from app.services.option_margin_calculator_resolver import (
    OptionMarginCalculatorResolver,
)
from app.services.realtime_fact_event_service import RealtimeFactEventService
from app.services.settlement_gate_service import SettlementGateService


@dataclass(frozen=True)
class OptionOrderMarginAdjustmentResult:
    """活动卖出开仓订单一次保证金检查结果。"""

    action: str
    order_id: str
    required_margin: Decimal = Decimal("0.000000")
    added_margin: Decimal = Decimal("0.000000")
    account_id: str = ""
    margin_risk_state: str = AccountRiskState.NORMAL.value
    frozen_margin: Decimal = Decimal("0.000000")


class OptionOrderMarginAdjustmentService:
    """
    重估活动期权卖出开仓订单，并在订单与账户行锁内补充冻结。

    行情Worker和成交结算共同复用``ensure_locked``，前者负责500ms周期
    提前补冻，后者负责成交前最后一道安全检查。两条链路均固定采用
    Order→Account的锁顺序，且所需保证金下降时不提前释放。
    """

    ACTIVE_STATUSES = {
        OrderStatus.ACCEPTED.value,
        OrderStatus.PARTIALLY_FILLED.value,
    }

    def __init__(
        self,
        *,
        market_tick_store: MarketTickStore,
        order_repository: OrderRepository | None = None,
        account_repository: AccountRepository | None = None,
        instrument_repository: InstrumentRepository | None = None,
        option_margin_resolver: OptionMarginCalculatorResolver | None = None,
        realtime_fact_events: RealtimeFactEventService | None = None,
        settlement_gate_service: SettlementGateService | None = None,
    ):
        self.market_tick_store = market_tick_store
        self.order_repository = order_repository or OrderRepository()
        self.account_repository = account_repository or AccountRepository()
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
        self.settlement_gate_service = (
            settlement_gate_service or SettlementGateService()
        )

    @classmethod
    def is_target_order(cls, order) -> bool:
        """只处理仍有剩余量的期权卖出开仓限价订单。"""

        return bool(
            order is not None
            and order.status in cls.ACTIVE_STATUSES
            and order.remaining_volume > 0
            and order.instrument_type in {
                InstrumentType.FUTURES_OPTION.value,
                InstrumentType.INDEX_OPTION.value,
            }
            and order.direction == OrderDirection.SELL.value
            and order.offset_flag == OffsetFlag.OPEN.value
            and order.order_type in {"LIMIT", "COUNTERPARTY", "LAST"}
        )

    def _underlying(self, db: Session, *, order, instrument):
        underlying = None
        if getattr(order, "underlying_order_book_id", None):
            underlying = self.instrument_repository.get_by_order_book_id(
                db, order.underlying_order_book_id
            )
        if (
            underlying is None
            and instrument is not None
            and instrument.underlying_instrument_id is not None
        ):
            underlying = self.instrument_repository.get_by_id(
                db, instrument.underlying_instrument_id
            )
        if underlying is None:
            raise DataAccessError(
                "活动期权订单的标的合约不存在",
                error_code="OPTION_UNDERLYING_NOT_FOUND",
            )
        return underlying

    def _calculate_required(self, db: Session, *, order, instrument):
        underlying = self._underlying(
            db,
            order=order,
            instrument=instrument,
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
            {option_key, underlying_key}
        )
        try:
            option_tick = MarketTickStore.mapping_to_tick(
                latest.get(option_key, {})
            )
            underlying_tick = MarketTickStore.mapping_to_tick(
                latest.get(underlying_key, {})
            )
        except Exception as exc:
            raise DataAccessError(
                "活动期权订单缺少有效期权或标的行情",
                error_code="OPTION_MARGIN_PRICE_UNAVAILABLE",
            ) from exc
        if (
            option_tick.last_price is None
            or option_tick.last_price <= 0
            or underlying_tick.last_price is None
            or underlying_tick.last_price <= 0
        ):
            raise DataAccessError(
                "活动期权订单缺少有效期权或标的行情",
                error_code="OPTION_MARGIN_PRICE_UNAVAILABLE",
            )

        rule = OptionMarginAdjustmentService._rule(order)
        if (
            order.margin_rule_id != rule.rule_id
            or order.margin_rule_version != rule.rule_version
        ):
            raise DataAccessError(
                "活动期权订单保证金规则版本不一致",
                error_code="OPTION_MARGIN_SNAPSHOT_VERSION_MISMATCH",
            )
        option_multiplier = Decimal(order.commission_contract_multiplier)
        underlying_multiplier = Decimal("1")
        underlying_margin_per_lot = Decimal("0.000000")
        if order.instrument_type == InstrumentType.FUTURES_OPTION.value:
            underlying_rate, underlying_multiplier = (
                OptionMarginAdjustmentService._commodity_underlying_inputs(
                    order
                )
            )
            underlying_margin_per_lot = quantize_money(
                underlying_tick.last_price
                * underlying_multiplier
                * underlying_rate
            )
        else:
            underlying_multiplier = Decimal(underlying.contract_multiplier)
        if option_multiplier <= 0 or underlying_multiplier <= 0:
            raise DataAccessError(
                "活动期权订单合约乘数快照不合法",
                error_code="OPTION_MARGIN_MULTIPLIER_INVALID",
            )
        option_price = max(Decimal(order.limit_price), option_tick.last_price)
        calculator = self.option_margin_resolver.resolve(
            instrument_type=order.instrument_type,
            exchange_id=instrument.exchange_id,
            margin_algorithm=rule.margin_algorithm,
        )
        result = calculator.calculate(
            OptionMarginInput(
                option_type=OptionType(instrument.option_type),
                strike_price=Decimal(instrument.strike_price),
                option_price=option_price,
                underlying_price=underlying_tick.last_price,
                option_multiplier=option_multiplier,
                underlying_multiplier=underlying_multiplier,
                volume=order.remaining_volume,
                price_mode=MarginPriceMode.REALTIME,
                calculated_at=utc_now(),
                rule=rule,
                underlying_margin_per_lot=underlying_margin_per_lot,
            )
        )
        return result

    def ensure_locked(
        self,
        db: Session,
        *,
        order,
        account,
        instrument,
        final_check: bool = False,
    ):
        """
        在调用方已经锁定Order和Account后执行最终检查，不自行提交事务。
        """

        if not self.is_target_order(order):
            return OptionOrderMarginAdjustmentResult(
                "NOT_APPLICABLE",
                getattr(order, "order_id", ""),
                account_id=getattr(order, "account_id", ""),
            )
        previous_order_risk = getattr(
            order,
            "margin_risk_state",
            AccountRiskState.NORMAL.value,
        )
        if final_check and (
            getattr(
                account,
                "risk_state",
                AccountRiskState.NORMAL.value,
            )
            != AccountRiskState.NORMAL.value
        ):
            # 成交事务不负责恢复账户风险，也不能静默清除订单风险来源。
            # 订单重估Worker会继续尝试补冻并可靠产生Account Dirty。
            return OptionOrderMarginAdjustmentResult(
                "RISK_BLOCKED",
                order.order_id,
                account_id=order.account_id,
                margin_risk_state=previous_order_risk,
                frozen_margin=quantize_money(order.frozen_margin),
            )
        try:
            result = self._calculate_required(
                db,
                order=order,
                instrument=instrument,
            )
        except DataAccessError:
            # 行情、规则或乘数任何一项不完整，都必须成为PostgreSQL中的
            # 可恢复风险事实。下一轮行情仍可重新进入本服务自愈，不能因
            # Account已经非NORMAL就在计算前永久拦截。
            order.margin_risk_state = (
                AccountRiskState.VALUATION_UNAVAILABLE.value
            )
            order.updated_at = utc_now()
            account.risk_state = (
                AccountRiskStateService.preserve_for_local_update(
                    getattr(
                        account,
                        "risk_state",
                        AccountRiskState.NORMAL.value,
                    ),
                    valuation_unavailable=True,
                )
            )
            account.updated_at = utc_now()
            return OptionOrderMarginAdjustmentResult(
                "VALUATION_UNAVAILABLE",
                order.order_id,
                account_id=order.account_id,
                margin_risk_state=order.margin_risk_state,
                frozen_margin=quantize_money(order.frozen_margin),
            )

        required = quantize_money(result.total_margin)
        current = quantize_money(order.frozen_margin)
        order.margin_price_mode = result.price_mode.value
        order.margin_underlying_price = result.underlying_price
        order.margin_option_price = result.option_price
        order.margin_calculation_version = result.calculation_version
        order.updated_at = utc_now()
        if required <= current:
            order.margin_risk_state = AccountRiskState.NORMAL.value
            return OptionOrderMarginAdjustmentResult(
                (
                    "RECOVERED"
                    if previous_order_risk
                    != AccountRiskState.NORMAL.value
                    else "SUFFICIENT"
                ),
                order.order_id,
                required,
                account_id=order.account_id,
                margin_risk_state=order.margin_risk_state,
                frozen_margin=quantize_money(order.frozen_margin),
            )

        delta = quantize_money(required - current)
        if (
            Decimal(account.available_cash) < delta
            or Decimal(account.risk_available_cash) < delta
        ):
            order.margin_risk_state = AccountRiskState.MARGIN_DEFICIT.value
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
            account.updated_at = utc_now()
            return OptionOrderMarginAdjustmentResult(
                "MARGIN_DEFICIT",
                order.order_id,
                required,
                account_id=order.account_id,
                margin_risk_state=order.margin_risk_state,
                frozen_margin=quantize_money(order.frozen_margin),
            )

        order.frozen_margin = required
        account.frozen_margin = quantize_money(account.frozen_margin + delta)
        account.available_cash = quantize_money(account.available_cash - delta)
        account.risk_available_cash = quantize_money(
            account.risk_available_cash - delta
        )
        order.margin_risk_state = AccountRiskState.NORMAL.value
        account.updated_at = utc_now()
        return OptionOrderMarginAdjustmentResult(
            "ADDED",
            order.order_id,
            required,
            delta,
            order.account_id,
            order.margin_risk_state,
            quantize_money(order.frozen_margin),
        )

    def adjust(self, db: Session, *, order_id: str):
        """独立事务重估一笔活动订单；不适用订单幂等返回。"""

        try:
            self.settlement_gate_service.ensure_trading_open(db)
            order = self.order_repository.get_by_order_id_for_update(
                db, order_id
            )
            if not self.is_target_order(order):
                db.commit()
                return OptionOrderMarginAdjustmentResult(
                    "NOT_APPLICABLE", order_id
                )
            account = self.account_repository.get_by_account_id_for_update(
                db, order.account_id
            )
            instrument = self.instrument_repository.get_by_order_book_id(
                db, order.order_book_id
            )
            if account is None or instrument is None:
                raise DataAccessError(
                    "活动期权订单的账户或合约不存在",
                    error_code="OPTION_ORDER_CONTEXT_INCOMPLETE",
                )
            previous_order_fact = (
                quantize_money(order.frozen_margin),
                order.margin_risk_state,
            )
            previous_account_frozen_margin = quantize_money(
                account.frozen_margin
            )
            result = self.ensure_locked(
                db,
                order=order,
                account=account,
                instrument=instrument,
            )
            occurred_at = order.updated_at
            current_order_fact = (
                quantize_money(order.frozen_margin),
                order.margin_risk_state,
            )
            if current_order_fact != previous_order_fact:
                # ORDER_MARGIN_UPDATED经现有投影转换为ORDER_UPDATED，使用
                # Outbox主键作为订单聚合单调版本，迟到事件不会覆盖新事实。
                self.realtime_fact_events.create_order_margin_updated(
                    db,
                    order=order,
                    occurred_at=occurred_at,
                )
            if (
                quantize_money(account.frozen_margin)
                != previous_account_frozen_margin
            ):
                self.realtime_fact_events.create_account_updated(
                    db,
                    account=account,
                    occurred_at=occurred_at,
                    fact_reason="OPTION_ORDER_MARGIN_ADJUSTMENT",
                )
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise
