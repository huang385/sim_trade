from app.common.exceptions import BusinessRuleError
from app.enums.option_enums import InstrumentType, OptionMarginAlgorithm
from app.services.cffex_index_option_margin_calculator import (
    CffexIndexOptionMarginCalculator,
)
from app.services.commodity_option_margin_calculator import (
    CommodityFuturesOptionMarginCalculator,
)
from app.services.option_margin_calculator import OptionMarginCalculator


class OptionMarginCalculatorResolver:
    """按照合约类型和规则算法选择纯计算器。"""

    def __init__(self):
        self._commodity = CommodityFuturesOptionMarginCalculator()
        self._cffex = CffexIndexOptionMarginCalculator()

    def resolve(
        self,
        *,
        instrument_type: str,
        exchange_id: str,
        margin_algorithm: str,
    ) -> OptionMarginCalculator:
        _ = exchange_id
        if (
            instrument_type == InstrumentType.FUTURES_OPTION.value
            and margin_algorithm
            == OptionMarginAlgorithm.COMMODITY_FUTURES_OPTION.value
        ):
            return self._commodity
        if (
            instrument_type == InstrumentType.INDEX_OPTION.value
            and margin_algorithm
            == OptionMarginAlgorithm.CFFEX_INDEX_OPTION.value
        ):
            return self._cffex
        raise BusinessRuleError(
            "没有匹配的期权保证金算法",
            error_code="OPTION_MARGIN_ALGORITHM_NOT_FOUND",
        )

