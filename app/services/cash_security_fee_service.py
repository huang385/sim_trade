"""现金证券费用快照的计算，不依赖开平仓语义。"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from app.common.decimal_utils import quantize_money
from app.common.exceptions import BusinessValidationError
from app.enums.reference_data_enums import CommissionType


@dataclass(frozen=True)
class CashSecurityFeeComponent:
    fee_type: str
    rule_item_id: int
    rule_version: str
    direction: str
    calculation_type: str
    commission_parameter: Decimal
    minimum_fee: Decimal
    aggregation_scope: str
    contract_multiplier: Decimal
    data_source: str


class CashSecurityFeeService:
    @staticmethod
    def calculate_component(
        *,
        price: Decimal,
        volume: int,
        calculation_type: str,
        commission_parameter: Decimal,
        contract_multiplier: Decimal,
    ) -> Decimal:
        try:
            commission_type = CommissionType(calculation_type)
        except ValueError as exc:
            raise BusinessValidationError(
                "不支持的现金证券手续费计算方式",
                error_code="UNSUPPORTED_COMMISSION_TYPE",
            ) from exc
        if commission_type == CommissionType.BY_VOLUME:
            result = Decimal(volume) * commission_parameter
        else:
            result = price * Decimal(volume) * contract_multiplier * commission_parameter
        return quantize_money(result)

    @classmethod
    def calculate_components(
        cls,
        *,
        price: Decimal,
        volume: int,
        components: Sequence[CashSecurityFeeComponent],
    ) -> Decimal:
        total = Decimal("0")
        seen_types: set[str] = set()
        for component in components:
            if component.fee_type in seen_types:
                raise BusinessValidationError(
                    "同一订单不能解析到重复手续费类型",
                    error_code="DUPLICATE_CASH_SECURITY_FEE_COMPONENT",
                )
            seen_types.add(component.fee_type)
            raw_fee = cls.calculate_component(
                price=price,
                volume=volume,
                calculation_type=component.calculation_type,
                commission_parameter=component.commission_parameter,
                contract_multiplier=component.contract_multiplier,
            )
            total += max(raw_fee, quantize_money(component.minimum_fee))
        return quantize_money(total)
