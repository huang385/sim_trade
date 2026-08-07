from dataclasses import dataclass
from datetime import date
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
    # 以下字段仅供统一期货/期权估值使用；默认值保证历史期货测试桩和
    # 旧调用方不需要同步修改。
    instrument_type: str = "FUTURES"
    total_volume: int = 0
    persisted_realtime_required_margin: Decimal = Decimal("0")
    persisted_used_margin: Decimal = Decimal("0")
    option_type: str | None = None
    strike_price: Decimal | None = None
    underlying_exchange_id: str | None = None
    underlying_symbol: str | None = None
    underlying_order_book_id: str | None = None
    margin_rule_snapshot: tuple[tuple[str, str], ...] = ()
    # 本轮实际读取的PostgreSQL持仓事实Outbox版本。
    source_fact_version: str = "0"
    # 账户现金是否已经吸收过至少一次逐日盯市。仅影响账户权益口径，累计
    # 浮盈展示仍始终相对原始开仓价。
    uses_settlement_basis: bool = False
    trading_day: date | None = None

    @property
    def underlying_key(self) -> tuple[str, str] | None:
        if not self.underlying_exchange_id or not self.underlying_symbol:
            return None
        return (
            self.underlying_exchange_id.strip().upper(),
            self.underlying_symbol.strip().upper(),
        )


@dataclass(frozen=True)
class PositionPnlResult:
    """按最新价重新计算得到的持仓绝对盈亏。"""

    cumulative_unrealized_pnl: Decimal
    daily_position_pnl: Decimal
    cash_unrealized_pnl: Decimal = Decimal("0")


@dataclass(frozen=True)
class ClosePnlResult:
    """一笔平仓消费同时对应的累计和当日盈亏。"""

    realized_pnl: Decimal
    daily_close_pnl: Decimal


class PnlCalculator:
    """
    期货盘中盈亏纯计算器。

    本类只执行Decimal运算，不访问PostgreSQL、Redis或系统时间。累计展示
    口径始终使用原始开仓价；当日及账户资金估值口径使用最近一次现金盯市
    后的pnl_base_price。首个交易日两者相同，日终后分离以避免现金和浮盈
    重复计入权益。
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

        cumulative = quantize_money(cumulative)
        daily = quantize_money(daily)
        return PositionPnlResult(
            cumulative_unrealized_pnl=cumulative,
            daily_position_pnl=daily,
            cash_unrealized_pnl=(
                daily if snapshot.uses_settlement_basis else cumulative
            ),
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

        for name, value in (
            ("平仓价", close_price),
            ("原始开仓价", open_price),
            ("盈亏基准价", pnl_base_price),
            ("合约乘数", contract_multiplier),
        ):
            cls._validate_decimal(name, value)
        try:
            direction = PositionDirection(position_direction)
        except ValueError as exc:
            raise BusinessValidationError(
                "不支持的持仓方向",
                error_code="INVALID_POSITION_DIRECTION",
            ) from exc
        if (
            close_price <= 0
            or open_price <= 0
            or pnl_base_price <= 0
            or contract_multiplier <= 0
            or volume <= 0
        ):
            raise BusinessValidationError(
                "平仓盈亏输入不合法", error_code="INVALID_PNL_INPUT"
            )
        sign = Decimal("1") if direction == PositionDirection.LONG else Decimal("-1")
        quantity = Decimal(volume) * contract_multiplier
        return ClosePnlResult(
            realized_pnl=quantize_money(
                (close_price - open_price) * quantity * sign
            ),
            daily_close_pnl=quantize_money(
                (close_price - pnl_base_price) * quantity * sign
            ),
        )
