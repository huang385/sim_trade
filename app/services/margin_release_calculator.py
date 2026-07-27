from decimal import Decimal

from app.common.decimal_utils import quantize_money
from app.common.exceptions import DataAccessError


class MarginReleaseCalculator:
    """按实际被平持仓明细计算保证金释放额的纯计算器。"""

    @staticmethod
    def calculate(
        *,
        remaining_margin: Decimal,
        close_volume: int,
        remaining_volume_before_close: int,
    ) -> Decimal:
        if (
            remaining_margin < 0
            or close_volume <= 0
            or remaining_volume_before_close <= 0
            or close_volume > remaining_volume_before_close
        ):
            raise DataAccessError(
                "持仓明细保证金释放参数不一致",
                error_code="POSITION_MARGIN_INCONSISTENT",
            )
        if close_volume == remaining_volume_before_close:
            return quantize_money(remaining_margin)
        return quantize_money(
            remaining_margin
            * Decimal(close_volume)
            / Decimal(remaining_volume_before_close)
        )
