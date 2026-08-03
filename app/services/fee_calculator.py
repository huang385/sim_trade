from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from app.common.decimal_utils import quantize_money
from app.common.exceptions import BusinessValidationError
from app.enums.order_enums import OffsetFlag
from app.enums.reference_data_enums import CommissionType
from app.models.fee_rule import FeeRule
from app.models.instrument import Instrument


@dataclass(frozen=True)
class FeeRuleSnapshot:
    """订单接受时固定的方向化手续费规则。"""

    rule_id: int
    rule_version: str
    instrument_type: str
    direction: str
    offset_flag: str
    commission_type: str
    commission_parameter: Decimal
    contract_multiplier: Decimal
    data_source: str

    def to_json_mapping(self) -> dict[str, str | int]:
        return {
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "instrument_type": self.instrument_type,
            "direction": self.direction,
            "offset_flag": self.offset_flag,
            "commission_type": self.commission_type,
            "commission_parameter": format(
                self.commission_parameter, "f"
            ),
            "contract_multiplier": format(
                self.contract_multiplier, "f"
            ),
            "data_source": self.data_source,
        }


@dataclass(frozen=True)
class FeeBucketKey:
    """
    手续费桶的稳定分组键。

    resolved_offset_flag 用于区分平今和平昨；其余三个字段固定手续费计算
    口径。同一桶内无论来自一条还是多条 PositionDetail，都只按总数量
    计算、量化一次手续费。
    """

    resolved_offset_flag: str
    commission_type: str
    commission_parameter: Decimal
    commission_contract_multiplier: Decimal


@dataclass(frozen=True)
class FeeBucketEntry:
    """参与手续费桶计算的一条明细及其数量。"""

    key: FeeBucketKey
    volume: int


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

    @classmethod
    def calculate_bucket_allocations(
        cls,
        *,
        price: Decimal,
        entries: Sequence[FeeBucketEntry],
    ) -> list[Decimal]:
        """
        按手续费桶汇总计算，再把桶级金额稳定分配回各条明细。

        每个桶只调用一次手续费公式。桶内按照输入顺序使用累计数量比例
        分配，最后一条明细消费桶内全部剩余金额，从而保证：

            sum(明细手续费) == 桶级手续费

        累计比例而不是逐条独立四舍五入，可以避免明细数量改变总手续费，
        也不会出现多个向上舍入导致最后一条得到负数的情况。
        """

        if not entries:
            return []

        grouped_indices: dict[FeeBucketKey, list[int]] = {}
        for index, entry in enumerate(entries):
            if not isinstance(entry.volume, int) or entry.volume <= 0:
                raise BusinessValidationError(
                    "手续费桶数量必须是大于0的整数",
                    error_code="INVALID_FEE_VOLUME",
                )
            # 枚举值统一转成数据库保存的字符串，避免 Enum 和 str 形成
            # 两个看似相同、实际哈希不同的手续费桶。
            try:
                normalized_type = CommissionType(
                    entry.key.commission_type
                ).value
            except ValueError as exc:
                raise BusinessValidationError(
                    "不支持的手续费计算方式",
                    error_code="UNSUPPORTED_COMMISSION_TYPE",
                ) from exc
            normalized_key = FeeBucketKey(
                resolved_offset_flag=entry.key.resolved_offset_flag,
                commission_type=normalized_type,
                commission_parameter=entry.key.commission_parameter,
                commission_contract_multiplier=(
                    entry.key.commission_contract_multiplier
                ),
            )
            grouped_indices.setdefault(normalized_key, []).append(index)

        result = [Decimal("0.000000") for _ in entries]
        for key, indices in grouped_indices.items():
            total_volume = sum(entries[index].volume for index in indices)
            bucket_commission = cls.calculate_from_snapshot(
                price=price,
                volume=total_volume,
                commission_type=key.commission_type,
                commission_parameter=key.commission_parameter,
                contract_multiplier=key.commission_contract_multiplier,
            )

            allocated = Decimal("0.000000")
            cumulative_volume = 0
            for position, index in enumerate(indices):
                entry_volume = entries[index].volume
                cumulative_volume += entry_volume
                if position == len(indices) - 1:
                    # 最后一条吸收桶级六位量化尾差，确保明细汇总严格等于桶。
                    share = quantize_money(bucket_commission - allocated)
                else:
                    cumulative_amount = quantize_money(
                        bucket_commission
                        * Decimal(cumulative_volume)
                        / Decimal(total_volume)
                    )
                    share = quantize_money(cumulative_amount - allocated)
                    allocated = cumulative_amount
                result[index] = share

        return result
