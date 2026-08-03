from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Integer,
    JSON,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.common.time_utils import utc_now
from app.core.database import Base


def default_pnl_base_price(context) -> Decimal:
    """旧测试构造未显式传值时，默认以真实开仓价作为当日盈亏基准。"""

    return Decimal(context.get_current_parameters()["open_price"])


class PositionDetail(Base):
    """
    逐笔开仓持仓明细。

    每一条开仓Trade只创建一条明细，用于保留具体开仓日、开仓价、
    剩余数量和原始保证金。后续实现平今、平昨时，汇总持仓不足以判断
    应该减少哪一笔持仓，因此必须保留该逐笔层数据。
    """

    __tablename__ = "position_detail"
    __table_args__ = (
        UniqueConstraint(
            "position_detail_id", name="uq_position_detail_detail_id"
        ),
        UniqueConstraint("open_trade_id", name="uq_position_detail_open_trade"),
        CheckConstraint(
            "original_volume > 0", name="ck_position_detail_original_positive"
        ),
        CheckConstraint(
            "remaining_volume >= 0", name="ck_position_detail_remaining_nonnegative"
        ),
        CheckConstraint(
            "frozen_volume >= 0", name="ck_position_detail_frozen_nonnegative"
        ),
        CheckConstraint(
            "remaining_volume <= original_volume",
            name="ck_position_detail_remaining_limit",
        ),
        CheckConstraint(
            "frozen_volume <= remaining_volume",
            name="ck_position_detail_frozen_limit",
        ),
        CheckConstraint(
            "remaining_margin >= 0",
            name="ck_position_detail_remaining_margin_nonnegative",
        ),
        CheckConstraint(
            "initial_occupied_margin >= 0",
            name="ck_position_detail_initial_margin_nonnegative",
        ),
        CheckConstraint(
            "realtime_required_margin >= 0",
            name="ck_position_detail_realtime_margin_nonnegative",
        ),
        CheckConstraint(
            "multiplier_snapshot > 0",
            name="ck_position_detail_multiplier_positive",
        ),
        CheckConstraint(
            "pnl_base_price > 0",
            name="ck_position_detail_pnl_base_price_positive",
        ),
    )

    # 数据库内部自增主键
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # 系统生成的全局持仓明细编号
    position_detail_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # 对应的持仓汇总编号
    position_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # 持仓所属账户编号
    account_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # 形成这笔持仓明细的成交编号；唯一约束防止重复创建
    open_trade_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # 标准合约编号
    order_book_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # 交易所代码
    exchange_id: Mapped[str] = mapped_column(String(32), nullable=False)

    # 系统内部合约代码
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)

    # 开仓成交时固定的精确合约类型。
    instrument_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="FUTURES",
        index=True,
    )

    # 持仓方向：LONG或SHORT
    direction: Mapped[str] = mapped_column(String(16), nullable=False)

    # 该笔持仓形成时的交易日
    open_trading_day: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # 该笔持仓的实际开仓成交价
    open_price: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)

    # 当日持仓和平仓盈亏计算基准。今仓等于开仓价；未来日终结算会把
    # 剩余持仓更新为正式结算价，但绝不覆盖上面的原始 open_price。
    pnl_base_price: Mapped[Decimal] = mapped_column(
        Numeric(24, 6),
        nullable=False,
        default=default_pnl_base_price,
    )

    # 原始开仓数量，创建后不再修改
    original_volume: Mapped[int] = mapped_column(Integer, nullable=False)

    # 尚未被平仓的数量，平仓成交后按实际消费数量递减
    remaining_volume: Mapped[int] = mapped_column(Integer, nullable=False)

    # 被尚未完成的平仓订单冻结的明细数量
    frozen_volume: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # 该笔成交从订单冻结保证金中分配到的金额
    open_margin: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)

    # 当前尚未随平仓释放的保证金；open_margin保留原始审计值不再修改
    remaining_margin: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), nullable=False
    )

    # 与open_margin语义相同的统一账户审计名称。保留open_margin兼容现有
    # 期货结算，后续代码使用本字段表达期权原始占用保证金。
    initial_occupied_margin: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), nullable=False, default=Decimal("0")
    )
    realtime_required_margin: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), nullable=False, default=Decimal("0")
    )
    margin_rule_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    margin_rule_version: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    margin_rule_snapshot: Mapped[dict | None] = mapped_column(
        JSON, nullable=True
    )
    margin_price_mode: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    margin_underlying_price: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 6), nullable=True
    )
    margin_option_price: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 6), nullable=True
    )
    margin_calculated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    multiplier_snapshot: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), nullable=False, default=Decimal("1")
    )

    # 该笔成交实际确认的开仓手续费
    open_commission: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)

    # 明细状态；剩余数量归零后更新为CLOSED
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN")

    # 明细创建时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )

    # 后续平仓修改剩余数量时的更新时间
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
