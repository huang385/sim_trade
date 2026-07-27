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

        try:
            normalized_offset_flag = OffsetFlag(offset_flag)
        except ValueError as exc:
            raise BusinessValidationError(
                "不支持的开平标志",
                error_code="UNSUPPORTED_OFFSET_FLAG",
            ) from exc

        # 根据开平类型选择手续费参数；下单冻结和成交重算共用同一入口，
        # 以免两个阶段对 CLOSE_TODAY/CLOSE_YESTERDAY 的解释发生偏差。
        commission = cls.resolve_commission_parameter(
            offset_flag=normalized_offset_flag,
            fee_rule=fee_rule,
        )

        return cls.calculate_from_snapshot(
            price=price,
            volume=volume,
            commission_type=fee_rule.commission_type,
            commission_parameter=commission,
            contract_multiplier=instrument.contract_multiplier,
        )

    @staticmethod
    def resolve_commission_parameter(
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

    @staticmethod
    def calculate_from_snapshot(
        *,
        price: Decimal,
        volume: int,
        commission_type: CommissionType | str,
        commission_parameter: Decimal,
        contract_multiplier: Decimal,
    ) -> Decimal:
        """
        使用订单接受时保存的规则快照计算手续费。

        下单阶段传入限价得到预计冻结手续费；成交阶段传入实际成交价得到
        Trade 的实际手续费。该方法只做 Decimal 计算，不读取当前规则表，
        因而规则同步更新不会改变已经接受订单的结算口径。
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
        if not isinstance(commission_parameter, Decimal):
            raise BusinessValidationError(
                "手续费参数必须使用Decimal类型",
                error_code="INVALID_COMMISSION_TYPE",
            )
        if commission_parameter < Decimal("0"):
            raise BusinessValidationError(
                "手续费参数不能小于0",
                error_code="INVALID_COMMISSION",
            )
        if not isinstance(contract_multiplier, Decimal):
            raise BusinessValidationError(
                "合约乘数必须使用Decimal类型",
                error_code="INVALID_CONTRACT_MULTIPLIER_TYPE",
            )
        if contract_multiplier <= Decimal("0"):
            raise BusinessValidationError(
                "合约乘数必须大于0",
                error_code="INVALID_CONTRACT_MULTIPLIER",
            )

        try:
            normalized_type = CommissionType(commission_type)
        except ValueError as exc:
            raise BusinessValidationError(
                "不支持的手续费计算方式",
                error_code="UNSUPPORTED_COMMISSION_TYPE",
            ) from exc

        if normalized_type == CommissionType.BY_VOLUME:
            fee = Decimal(volume) * commission_parameter
        else:
            fee = (
                price
                * Decimal(volume)
                * contract_multiplier
                * commission_parameter
            )

        # 当前版本不额外应用 discount_rate；规则同步时写入的参数应当已经
        # 表示最终费率，避免订单接受和成交阶段重复折扣。
        return quantize_money(fee)
