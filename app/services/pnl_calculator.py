from dataclasses import dataclass
from decimal import Decimal

from app.common.decimal_utils import quantize_money
from app.common.exceptions import BusinessValidationError
from app.enums.order_enums import PositionDirection


@dataclass(frozen=True)
class PnlDetailSnapshot:
    """与SQLAlchemy Session解耦的逐笔持仓盈亏计算输入。"""

    position_detail_id: str
    open_price: Decimal
    pnl_base_price: Decimal
    remaining_volume: int


@dataclass(frozen=True)
class PositionPnlSnapshot:
    """一条活动持仓及其有效明细的不可变计算快照。"""

    position_id: str
    account_id: str
    order_book_id: str
    exchange_id: str
    symbol: str
    direction: str
    contract_multiplier: Decimal
    persisted_unrealized_pnl: Decimal
    persisted_daily_position_pnl: Decimal
    details: tuple[PnlDetailSnapshot, ...]


@dataclass(frozen=True)
class PositionPnlResult:
    """按最新价重新计算得到的持仓绝对盈亏。"""

    cumulative_unrealized_pnl: Decimal
    daily_position_pnl: Decimal


@dataclass(frozen=True)
class ClosePnlResult:
    """一笔平仓消费同时对应的累计和当日盈亏。"""

    realized_pnl: Decimal
    daily_close_pnl: Decimal


class PnlCalculator:
    """
    期货盘中盈亏纯计算器。

    本类只执行Decimal运算，不访问PostgreSQL、Redis或系统时间。累计口径
    使用原始开仓价，当日口径使用可由未来日终结算更新的pnl_base_price。
    """

    @staticmethod
    def _validate_decimal(name: str, value: Decimal) -> None:
        if not isinstance(value, Decimal):
            raise BusinessValidationError(
                f"{name}必须使用Decimal类型",
                error_code="INVALID_PNL_DECIMAL_TYPE",
            )

    @classmethod
    def calculate_position(
        cls,
        *,
        mark_price: Decimal,
        snapshot: PositionPnlSnapshot,
    ) -> PositionPnlResult:
        """逐条有效PositionDetail计算并汇总持仓的两套浮动盈亏。"""

        cls._validate_decimal("盯市价格", mark_price)
        cls._validate_decimal("合约乘数", snapshot.contract_multiplier)
        if mark_price <= 0 or snapshot.contract_multiplier <= 0:
            raise BusinessValidationError(
                "盯市价格和合约乘数必须大于0",
                error_code="INVALID_PNL_INPUT",
            )
        try:
            direction = PositionDirection(snapshot.direction)
        except ValueError as exc:
            raise BusinessValidationError(
                "不支持的持仓方向",
                error_code="INVALID_POSITION_DIRECTION",
            ) from exc

        cumulative = Decimal("0")
        daily = Decimal("0")
        for detail in snapshot.details:
            if detail.remaining_volume <= 0:
                continue
            cls._validate_decimal("原始开仓价", detail.open_price)
            cls._validate_decimal("盈亏基准价", detail.pnl_base_price)
            if detail.open_price <= 0 or detail.pnl_base_price <= 0:
                raise BusinessValidationError(
                    "开仓价和盈亏基准价必须大于0",
                    error_code="INVALID_PNL_INPUT",
                )
            sign = (
                Decimal("1")
                if direction == PositionDirection.LONG
                else Decimal("-1")
            )
            quantity = (
                Decimal(detail.remaining_volume)
                * snapshot.contract_multiplier
            )
            cumulative += (
                (mark_price - detail.open_price) * quantity * sign
            )
            daily += (
                (mark_price - detail.pnl_base_price) * quantity * sign
            )

        return PositionPnlResult(
            cumulative_unrealized_pnl=quantize_money(cumulative),
            daily_position_pnl=quantize_money(daily),
        )

    @classmethod
    def calculate_close(
        cls,
        *,
        position_direction: str,
        close_price: Decimal,
        open_price: Decimal,
        pnl_base_price: Decimal,
        volume: int,
        contract_multiplier: Decimal,
    ) -> ClosePnlResult:
        """计算一笔平仓相对原始开仓价和当日基准价的两套结果。"""

        snapshot = PositionPnlSnapshot(
            position_id="",
            account_id="",
            order_book_id="",
            exchange_id="",
            symbol="",
            direction=position_direction,
            contract_multiplier=contract_multiplier,
            persisted_unrealized_pnl=Decimal("0"),
            persisted_daily_position_pnl=Decimal("0"),
            details=(
                PnlDetailSnapshot(
                    position_detail_id="",
                    open_price=open_price,
                    pnl_base_price=pnl_base_price,
                    remaining_volume=volume,
                ),
            ),
        )
        result = cls.calculate_position(
            mark_price=close_price,
            snapshot=snapshot,
        )
        return ClosePnlResult(
            realized_pnl=result.cumulative_unrealized_pnl,
            daily_close_pnl=result.daily_position_pnl,
        )
