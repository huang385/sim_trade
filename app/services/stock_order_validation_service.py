from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessRuleError, BusinessValidationError
from app.enums.instrument_enums import InstrumentType
from app.enums.order_enums import PositionDirection
from app.enums.reference_data_enums import StockPriceLimitType
from app.models.instrument import Instrument
from app.models.position import Position
from app.models.stock_daily_trading_fact import StockDailyTradingFact
from app.models.stock_trading_rule import StockTradingRule
from app.repositories.stock_daily_trading_fact_repository import (
    StockDailyTradingFactRepository,
)
from app.repositories.stock_trading_rule_repository import (
    StockTradingRuleRepository,
)
from app.schemas.order_schema import StockOrderCreateRequest
from app.services.order_validation_service import OrderValidationService


@dataclass(frozen=True)
class StockOrderReference:
    instrument: Instrument
    rule: StockTradingRule
    daily_fact: StockDailyTradingFact


class StockOrderValidationService:
    """股票订单的静态规则、每日事实及买卖数量校验。"""

    def __init__(
        self,
        rule_repository: StockTradingRuleRepository | None = None,
        fact_repository: StockDailyTradingFactRepository | None = None,
    ) -> None:
        self.rule_repository = rule_repository or StockTradingRuleRepository()
        self.fact_repository = fact_repository or StockDailyTradingFactRepository()

    def resolve_and_validate(
        self,
        db: Session,
        *,
        instrument: Instrument | None,
        request: StockOrderCreateRequest,
        trading_day: date,
    ) -> StockOrderReference:
        if instrument is None:
            raise BusinessRuleError(
                "股票合约不存在", error_code="STOCK_INSTRUMENT_NOT_FOUND"
            )
        if instrument.instrument_type != InstrumentType.STOCK.value:
            raise BusinessRuleError(
                "股票接口只能交易 STOCK Instrument",
                error_code="STOCK_INSTRUMENT_TYPE_INVALID",
            )
        if instrument.market_type != "STOCK":
            raise BusinessRuleError(
                "股票合约市场类型不正确",
                error_code="STOCK_MARKET_TYPE_INVALID",
            )
        if not instrument.is_active or not instrument.is_tradeable:
            raise BusinessRuleError(
                "股票合约当前不可交易",
                error_code="STOCK_INSTRUMENT_NOT_TRADEABLE",
            )
        try:
            rule = self.rule_repository.resolve_for_trading_day(
                db, instrument_id=instrument.id, trading_day=trading_day
            )
        except LookupError as exc:
            raise BusinessRuleError(
                "当前交易日不存在唯一股票交易规则",
                error_code="STOCK_TRADING_RULE_UNAVAILABLE",
            ) from exc
        fact = self.fact_repository.get(
            db, instrument_id=instrument.id, trading_day=trading_day
        )
        if fact is None:
            raise BusinessRuleError(
                "当前交易日缺少股票交易事实",
                error_code="STOCK_DAILY_FACT_MISSING",
            )
        if fact.is_suspended or not fact.is_tradeable:
            raise BusinessRuleError(
                "股票当前不可交易", error_code="STOCK_DAILY_NOT_TRADEABLE"
            )
        self._validate_limits(request.limit_price, rule, fact)
        self._validate_common(request=request, instrument=instrument)
        return StockOrderReference(instrument=instrument, rule=rule, daily_fact=fact)

    @staticmethod
    def _validate_common(
        *, request: StockOrderCreateRequest, instrument: Instrument
    ) -> None:
        if request.volume < instrument.min_volume:
            raise BusinessValidationError(
                "委托数量低于合约最小数量",
                error_code="VOLUME_BELOW_MINIMUM",
            )
        if request.volume > instrument.max_volume:
            raise BusinessValidationError(
                "委托数量高于合约最大数量",
                error_code="VOLUME_ABOVE_MAXIMUM",
            )
        OrderValidationService.validate_price_tick(
            price=request.limit_price, price_tick=instrument.price_tick
        )

    @staticmethod
    def _validate_limits(
        price: Decimal, rule: StockTradingRule, fact: StockDailyTradingFact
    ) -> None:
        if fact.upper_limit_price is None or fact.lower_limit_price is None:
            if rule.price_limit_type == StockPriceLimitType.NONE.value:
                return
            raise BusinessRuleError(
                "当前交易日缺少涨跌停价格",
                error_code="STOCK_PRICE_LIMIT_FACT_MISSING",
            )
        if price > fact.upper_limit_price or price < fact.lower_limit_price:
            raise BusinessValidationError(
                "委托价格超出当日涨跌停范围",
                error_code="STOCK_PRICE_LIMIT_EXCEEDED",
            )

    @staticmethod
    def validate_buy(
        *, request: StockOrderCreateRequest, rule: StockTradingRule
    ) -> None:
        if rule.buy_lot_size <= 0:
            raise BusinessRuleError(
                "股票买入单位无效", error_code="STOCK_BUY_LOT_INVALID"
            )
        if rule.buy_volume_must_be_multiple and request.volume % rule.buy_lot_size:
            raise BusinessValidationError(
                "买入数量必须为配置买入单位的整数倍",
                error_code="STOCK_BUY_LOT_MISMATCH",
            )

    @staticmethod
    def validate_sell(
        *,
        request: StockOrderCreateRequest,
        rule: StockTradingRule,
        position: Position | None,
    ) -> None:
        if position is None or position.direction != PositionDirection.LONG.value:
            raise BusinessRuleError(
                "股票卖出需要已有 LONG 持仓",
                error_code="STOCK_LONG_POSITION_REQUIRED",
            )
        if request.volume > position.available_volume:
            raise BusinessRuleError(
                "股票可卖数量不足", error_code="STOCK_AVAILABLE_VOLUME_INSUFFICIENT"
            )
        if rule.sell_min_unit <= 0:
            raise BusinessRuleError(
                "股票卖出单位无效", error_code="STOCK_SELL_UNIT_INVALID"
            )
        if not rule.sell_odd_lot_allowed and request.volume % rule.sell_min_unit:
            raise BusinessValidationError(
                "卖出数量必须为配置卖出单位的整数倍",
                error_code="STOCK_SELL_UNIT_MISMATCH",
            )
