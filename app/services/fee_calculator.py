from decimal import Decimal

from app.common.decimal_utils import quantize_money
from app.common.exceptions import BusinessValidationError
from app.enums.order_enums import OffsetFlag
from app.enums.reference_data_enums import CommissionType
from app.models.fee_rule import FeeRule
from app.models.instrument import Instrument


class FeeCalculator:
    """
    期货手续费计算器。

    可以用于：

    1. 下单前计算预计冻结手续费；
    2. 成交后根据实际成交价计算实际手续费。

    该类不访问数据库，也不修改账户。
    """

    @classmethod
    def calculate(
        cls,
        *,
        price: Decimal,
        volume: int,
        offset_flag: OffsetFlag,
        instrument: Instrument,
        fee_rule: FeeRule,
    ) -> Decimal:
        """
        计算手续费。

        BY_VOLUME：

            手续费 =
            成交手数 × 手续费参数

        BY_AMOUNT：

            手续费 =
            成交价格
            × 成交手数
            × 合约乘数
            × 手续费率
        """

        if not isinstance(price, Decimal):
            raise BusinessValidationError(
                "价格必须使用Decimal类型",
                error_code="INVALID_PRICE_TYPE",
            )

        if price <= Decimal("0"):
            raise BusinessValidationError(
                "手续费计算价格必须大于0",
                error_code="INVALID_FEE_PRICE",
            )

        if not isinstance(volume, int):
            raise BusinessValidationError(
                "手续费计算数量必须是整数",
                error_code="INVALID_VOLUME_TYPE",
            )

        if volume <= 0:
            raise BusinessValidationError(
                "手续费计算数量必须大于0",
                error_code="INVALID_FEE_VOLUME",
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

        try:
            normalized_offset_flag = OffsetFlag(offset_flag)
        except ValueError as exc:
            raise BusinessValidationError(
                "不支持的开平标志",
                error_code="UNSUPPORTED_OFFSET_FLAG",
            ) from exc

        # 根据开平类型选择手续费参数
        commission = cls._select_commission(
            offset_flag=normalized_offset_flag,
            fee_rule=fee_rule,
        )

        if commission is None:
            raise BusinessValidationError(
                "手续费参数不存在",
                error_code="COMMISSION_MISSING",
            )

        if commission < Decimal("0"):
            raise BusinessValidationError(
                "手续费参数不能小于0",
                error_code="INVALID_COMMISSION",
            )

        try:
            commission_type = CommissionType(
                fee_rule.commission_type
            )
        except ValueError as exc:
            raise BusinessValidationError(
                "不支持的手续费计算方式",
                error_code="UNSUPPORTED_COMMISSION_TYPE",
            ) from exc

        if commission_type == CommissionType.BY_VOLUME:
            # 按手数收费：
            # 手续费 = 手数 × 每手手续费
            fee = Decimal(volume) * commission

        elif commission_type == CommissionType.BY_AMOUNT:
            # 按成交金额比例收费：
            # 手续费 =
            # 价格 × 手数 × 合约乘数 × 手续费率
            fee = (
                price
                * Decimal(volume)
                * contract_multiplier
                * commission
            )

        else:
            raise BusinessValidationError(
                "不支持的手续费计算方式",
                error_code="UNSUPPORTED_COMMISSION_TYPE",
            )

        # 当前版本暂不使用discount_rate参与计算，
        # 防止字段含义未确认时发生重复折扣。
        return quantize_money(fee)

    @staticmethod
    def _select_commission(
        *,
        offset_flag: OffsetFlag,
        fee_rule: FeeRule,
    ) -> Decimal:
        """
        根据开平标志选择对应手续费参数。

        OPEN：
            开仓手续费。

        CLOSE：
            普通平仓手续费。

        CLOSE_TODAY：
            平今手续费。

        CLOSE_YESTERDAY：
            平昨通常使用普通平仓手续费。
        """

        if offset_flag == OffsetFlag.OPEN:
            return fee_rule.open_commission

        if offset_flag == OffsetFlag.CLOSE:
            return fee_rule.close_commission

        if offset_flag == OffsetFlag.CLOSE_TODAY:
            return fee_rule.close_today_commission

        if offset_flag == OffsetFlag.CLOSE_YESTERDAY:
            return fee_rule.close_commission

        raise BusinessValidationError(
            "不支持的开平标志",
            error_code="UNSUPPORTED_OFFSET_FLAG",
        )
