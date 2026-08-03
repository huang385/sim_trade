from app.common.exceptions import BusinessRuleError
from app.core.config import Settings, settings
from app.enums.account_enums import AccountRiskState, AccountType
from app.enums.option_enums import InstrumentType
from app.enums.order_enums import OffsetFlag, OrderDirection


class OptionTradingPermissionService:
    """统一校验系统产品开关、账户权限以及实时风险限制。"""

    def __init__(self, config: Settings = settings):
        self.config = config

    def validate(
        self,
        *,
        account,
        instrument,
        direction: OrderDirection,
        offset_flag: OffsetFlag,
    ) -> None:
        # 历史期货测试桩和升级前构造的合约对象可能尚未显式携带
        # instrument_type。数据库迁移会把真实历史数据回填为 FUTURES，
        # 这里同样按 FUTURES 兼容，避免期权扩展改变原期货链路。
        instrument_type = InstrumentType(
            getattr(instrument, "instrument_type", InstrumentType.FUTURES.value)
        )
        # 统一账户的风险状态属于账户级限制，必须先于具体产品开关判断。
        # 无论本次开仓的是期货、商品期权还是股指期权，只要账户估值不可用
        # 或风险可用资金已经不足，都不能继续增加风险；平仓仍然允许执行。
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

        if instrument_type == InstrumentType.FUTURES:
            return
        if instrument_type == InstrumentType.INDEX:
            raise BusinessRuleError(
                "指数合约不能提交交易订单",
                error_code="INDEX_NOT_TRADEABLE",
            )
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

        # 本阶段股指期权只允许买方开仓及卖出平多。卖出开仓无论配置值
        # 如何都失败关闭，避免误启用尚未接入指数行情的卖方保证金链路。
        if direction == OrderDirection.SELL and offset_flag == OffsetFlag.OPEN:
            raise BusinessRuleError(
                "股指期权卖出开仓暂未开放",
                error_code="INDEX_OPTION_SHORT_TRADING_UNAVAILABLE",
            )
        if not self.config.index_option_buy_trading_enabled:
            raise BusinessRuleError(
                "股指期权买方交易未开启",
                error_code="INDEX_OPTION_BUY_TRADING_NOT_ENABLED",
            )
        if not (
            (direction == OrderDirection.BUY and offset_flag == OffsetFlag.OPEN)
            or (
                direction == OrderDirection.SELL
                and offset_flag
                in {
                    OffsetFlag.CLOSE,
                    OffsetFlag.CLOSE_TODAY,
                    OffsetFlag.CLOSE_YESTERDAY,
                }
            )
        ):
            raise BusinessRuleError(
                "本阶段只支持股指期权买方开平仓",
                error_code="INDEX_OPTION_SHORT_TRADING_UNAVAILABLE",
            )
