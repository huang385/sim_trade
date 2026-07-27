from dataclasses import dataclass
from datetime import date

from app.common.exceptions import BusinessRuleError
from app.enums.order_enums import OffsetFlag


@dataclass(frozen=True)
class PositionFreezePlan:
    """从一笔逐笔持仓明细冻结的数量。"""

    detail: object
    volume: int


class PositionCloseAllocator:
    """
    平仓冻结的确定性纯业务分配器。

    CLOSE_TODAY只选择今仓，CLOSE_YESTERDAY只选择昨仓；普通CLOSE采用
    全市场统一的第一版策略：昨仓优先、今仓补足，同类按明细id FIFO。
    """

    @classmethod
    def allocate(
        cls,
        *,
        details,
        offset_flag: OffsetFlag,
        trading_day: date,
        volume: int,
    ) -> list[PositionFreezePlan]:
        if volume <= 0:
            raise BusinessRuleError(
                "平仓数量必须大于0",
                error_code="INVALID_CLOSE_VOLUME",
            )

        available = [
            detail
            for detail in details
            if detail.remaining_volume - detail.frozen_volume > 0
        ]
        yesterday = sorted(
            (
                item
                for item in available
                if item.open_trading_day < trading_day
            ),
            key=lambda item: item.id,
        )
        today = sorted(
            (
                item
                for item in available
                if item.open_trading_day == trading_day
            ),
            key=lambda item: item.id,
        )

        if offset_flag == OffsetFlag.CLOSE_TODAY:
            candidates = today
            error_code = "INSUFFICIENT_TODAY_POSITION"
        elif offset_flag == OffsetFlag.CLOSE_YESTERDAY:
            candidates = yesterday
            error_code = "INSUFFICIENT_YESTERDAY_POSITION"
        elif offset_flag == OffsetFlag.CLOSE:
            candidates = yesterday + today
            error_code = "INSUFFICIENT_CLOSE_POSITION"
        else:
            raise BusinessRuleError(
                "不支持的平仓标志",
                error_code="UNSUPPORTED_OFFSET_FLAG",
            )

        remaining = volume
        plans: list[PositionFreezePlan] = []
        for detail in candidates:
            free_volume = detail.remaining_volume - detail.frozen_volume
            allocated = min(free_volume, remaining)
            if allocated > 0:
                plans.append(PositionFreezePlan(detail, allocated))
                remaining -= allocated
            if remaining == 0:
                break

        if remaining > 0:
            raise BusinessRuleError(
                "可平持仓数量不足",
                error_code=error_code,
            )
        return plans
