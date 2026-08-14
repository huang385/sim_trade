from app.common.exceptions import BusinessRuleError
from app.core.config import Settings, settings
from app.enums.account_enums import AccountRiskState, AccountType
from app.enums.option_enums import InstrumentType
from app.enums.order_enums import OffsetFlag, OrderDirection
from app.enums.product_enums import ProductFamily
from app.services.product_strategy_registry import (
    ProductStrategyRegistry,
    product_strategy_registry,
)


class OptionTradingPermissionService:
    """统一校验产品开关、账户权限以及实时风险限制。"""

    def __init__(
        self,
        config: Settings = settings,
        *,
        product_registry: ProductStrategyRegistry | None = None,
    ):
        self.config = config
        self.product_registry = product_registry or product_strategy_registry

    def validate(
        self,
        *,
        account,
        instrument,
        direction: OrderDirection,
        offset_flag: OffsetFlag,
    ) -> None:
        # 迁移前构造的历史合约可能没有 instrument_type；按期货兼容，
        # 避免期权扩展改变原有期货订单链路。
        instrument_type_value = instrument.instrument_type
        # 风险不足时禁止继续开仓增加风险，但必须允许已有仓位平仓。
        if (
            getattr(account, "risk_state", AccountRiskState.NORMAL.value)
            in {
                AccountRiskState.MARGIN_DEFICIT.value,
                AccountRiskState.VALUATION_UNAVAILABLE.value,
            }
            and offset_flag == OffsetFlag.OPEN
        ):
            raise BusinessRuleError(
                "账户风险状态禁止增加风险",
                error_code="ACCOUNT_RISK_INCREASE_BLOCKED",
            )

        if instrument_type_value == InstrumentType.INDEX.value:
            raise BusinessRuleError(
                "指数合约不能提交交易订单",
                error_code="INDEX_NOT_TRADEABLE",
            )
        product = self.product_registry.resolve(instrument_type_value)
        if product.family == ProductFamily.FUTURES:
            return
        instrument_type = InstrumentType(instrument_type_value)
        if account.account_type != AccountType.FUTURES.value:
            raise BusinessRuleError(
                "当前账户类型不支持期权交易",
                error_code="OPTION_ACCOUNT_TYPE_UNSUPPORTED",
            )
        if (
            not self.config.option_trading_enabled
            or not account.option_trading_enabled
        ):
            raise BusinessRuleError(
                "期权交易权限未开启",
                error_code="OPTION_TRADING_NOT_ENABLED",
            )
        if instrument_type == InstrumentType.FUTURES_OPTION:
            if not self.config.commodity_option_trading_enabled:
                raise BusinessRuleError(
                    "商品期权交易未开启",
                    error_code="COMMODITY_OPTION_TRADING_NOT_ENABLED",
                )
            return

        # 股指期权买方与卖方分别使用独立开关。卖出开仓及其买入平仓必须
        # 同时开放，避免账户能够建立空头却无法主动平仓。
        is_close = offset_flag in {
            OffsetFlag.CLOSE,
            OffsetFlag.CLOSE_TODAY,
            OffsetFlag.CLOSE_YESTERDAY,
        }
        is_long_side = (
            direction == OrderDirection.BUY and offset_flag == OffsetFlag.OPEN
        ) or (direction == OrderDirection.SELL and is_close)
        is_short_side = (
            direction == OrderDirection.SELL and offset_flag == OffsetFlag.OPEN
        ) or (direction == OrderDirection.BUY and is_close)
        if is_long_side and not self.config.index_option_buy_trading_enabled:
            raise BusinessRuleError(
                "股指期权买方交易未开启",
                error_code="INDEX_OPTION_BUY_TRADING_NOT_ENABLED",
            )
        if is_short_side and not self.config.index_option_short_trading_enabled:
            raise BusinessRuleError(
                "股指期权卖方交易未开启",
                error_code="INDEX_OPTION_SHORT_TRADING_NOT_ENABLED",
            )
