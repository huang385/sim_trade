from decimal import Decimal

from app.common.decimal_utils import quantize_money
from app.common.exceptions import BusinessValidationError
from app.enums.order_enums import OrderDirection
from app.models.instrument import Instrument
from app.models.margin_rule import MarginRule


class MarginCalculator:
    """
    期货开仓保证金计算器。

    该类是纯计算类：

    1. 不访问数据库；
    2. 不修改账户；
    3. 不冻结资金；
    4. 不提交事务。

    它只根据传入的规则计算保证金金额。
    """

    @staticmethod
    def calculate_open_margin(
        *,
        price: Decimal,
        volume: int,
        direction: OrderDirection,
        instrument: Instrument,
        margin_rule: MarginRule,
    ) -> Decimal:
        """
        计算期货开仓保证金。

        计算公式：

            保证金 =
            价格
            × 手数
            × 合约乘数
            × 保证金率

        BUY开仓：
            使用long_margin_rate。

        SELL开仓：
            使用short_margin_rate。
        """

        # 禁止直接使用float进行资金计算
        if not isinstance(price, Decimal):
            raise BusinessValidationError(
                "价格必须使用Decimal类型",
                error_code="INVALID_PRICE_TYPE",
            )

        if price <= Decimal("0"):
            raise BusinessValidationError(
                "开仓价格必须大于0",
                error_code="INVALID_OPEN_PRICE",
            )

        if not isinstance(volume, int):
            raise BusinessValidationError(
                "开仓数量必须是整数",
                error_code="INVALID_VOLUME_TYPE",
            )

        if volume <= 0:
            raise BusinessValidationError(
                "开仓数量必须大于0",
                error_code="INVALID_OPEN_VOLUME",
            )

        contract_multiplier = instrument.contract_multiplier

        if contract_multiplier is None:
            raise BusinessValidationError(
                "合约乘数不存在",
                error_code="CONTRACT_MULTIPLIER_MISSING",
            )

        if contract_multiplier <= Decimal("0"):
            raise BusinessValidationError(
                "合约乘数必须大于0",
                error_code="INVALID_CONTRACT_MULTIPLIER",
            )

        # 接受枚举或者合法字符串，
        # 非法值统一转换成业务异常。
        try:
            normalized_direction = OrderDirection(direction)
        except ValueError as exc:
            raise BusinessValidationError(
                "不支持的委托方向",
                error_code="UNSUPPORTED_ORDER_DIRECTION",
            ) from exc

        if normalized_direction == OrderDirection.BUY:
            margin_rate = margin_rule.long_margin_rate

        elif normalized_direction == OrderDirection.SELL:
            margin_rate = margin_rule.short_margin_rate

        else:
            # 理论上不会进入这里，保留防御性判断
            raise BusinessValidationError(
                "不支持的委托方向",
                error_code="UNSUPPORTED_ORDER_DIRECTION",
            )

        if margin_rate is None:
            raise BusinessValidationError(
                "保证金率不存在",
                error_code="MARGIN_RATE_MISSING",
            )

        if margin_rate < Decimal("0"):
            raise BusinessValidationError(
                "保证金率不能小于0",
                error_code="INVALID_MARGIN_RATE",
            )

        if margin_rate > Decimal("1"):
            raise BusinessValidationError(
                "保证金率不能大于1",
                error_code="INVALID_MARGIN_RATE",
            )

        margin = (
            price
            * Decimal(volume)
            * contract_multiplier
            * margin_rate
        )

        return quantize_money(margin)
