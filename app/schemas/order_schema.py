from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.common.code_utils import normalize_code
from app.enums.order_enums import (
    OffsetFlag,
    OrderDirection,
    OrderStatus,
    OrderSubmitStatus,
    OrderType,
)


class OrderCreateRequest(BaseModel):
    """
    创建订单请求。

    Pydantic 负责请求格式、枚举、正数和字符串长度等基础校验。
    合约状态、价格最小变动单位和数量上下限等业务校验，
    由 OrderValidationService 统一处理。
    """

    # 客户端订单编号。
    # 同一账户重复提交相同编号时返回原订单，不重复冻结资金。
    client_order_id: str = Field(min_length=1, max_length=64)

    # 模拟交易账户编号
    account_id: str = Field(min_length=1, max_length=64)

    # 交易所代码，例如 SHFE
    exchange_id: str = Field(min_length=1, max_length=32)

    # 合约代码，例如 RB2610
    symbol: str = Field(min_length=1, max_length=64)

    # 买卖方向。开仓 BUY 建立多头，SELL 建立空头。
    direction: OrderDirection

    # 开平标志。第一阶段只允许 OPEN。
    offset_flag: OffsetFlag

    # 订单类型。第一阶段只支持 LIMIT。
    order_type: OrderType = OrderType.LIMIT

    # 限价单委托价格，必须大于0，并符合合约 price_tick。
    limit_price: Decimal | None = Field(default=None, gt=Decimal("0"))

    # 委托数量，必须大于0，并处于合约允许的数量范围内。
    volume: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_price_by_type(self):
        if self.order_type == OrderType.LIMIT and self.limit_price is None:
            raise ValueError("限价单必须提供 limit_price")
        if self.order_type != OrderType.LIMIT and self.limit_price is not None:
            raise ValueError("非限价单不得提交 limit_price")
        return self

    @field_validator("exchange_id", "symbol", mode="before")
    @classmethod
    def normalize_codes(cls, value: str) -> str:
        """统一去除首尾空格并转换为大写代码。"""

        return normalize_code(value)

    @field_validator("client_order_id", "account_id", mode="before")
    @classmethod
    def strip_identifiers(cls, value: str) -> str:
        """去除客户端编号和账户编号首尾的无效空格。"""

        if not isinstance(value, str):
            return value
        return value.strip()


class OrderCancelRequest(BaseModel):
    """
    主动撤销订单请求。

    account_id 仅用于确认订单归属，不代表完整权限认证；后续接入身份系统后，
    应改为从认证上下文校验账户操作权限。
    """

    account_id: str = Field(min_length=1, max_length=64)

    @field_validator("account_id", mode="before")
    @classmethod
    def strip_account_id(cls, value: str) -> str:
        """去除账户编号首尾空格，空字符串由 Field 长度校验拒绝。"""

        if not isinstance(value, str):
            return value
        return value.strip()


class OrderResponse(BaseModel):
    """
    订单响应结构。

    支持直接从 SQLAlchemy Order 对象读取字段。
    金额字段继续使用 Decimal，防止返回过程中引入浮点误差。
    """

    model_config = ConfigDict(from_attributes=True)

    # 系统订单编号
    order_id: str

    # 客户端订单编号
    client_order_id: str

    # 下单账户编号
    account_id: str

    # 标准合约编号
    order_book_id: str

    # 交易所代码
    exchange_id: str

    # 合约代码
    symbol: str

    # 订单所属交易日
    trading_day: date
    instrument_type: str = "FUTURES"

    # 买卖、开平和订单类型
    direction: OrderDirection
    offset_flag: OffsetFlag
    order_type: OrderType

    # 委托价格与数量执行情况
    limit_price: Decimal
    submitted_limit_price: Decimal | None = None
    resolved_price: Decimal | None = None
    market_protection_price: Decimal | None = None
    price_snapshot_time: datetime | None = None
    price_snapshot_source: str | None = None
    price_snapshot_bid1: Decimal | None = None
    price_snapshot_ask1: Decimal | None = None
    price_snapshot_last: Decimal | None = None
    total_volume: int
    traded_volume: int
    remaining_volume: int
    cancelled_volume: int
    average_price: Decimal | None

    # 当前订单冻结的资金和持仓资源
    frozen_margin: Decimal
    frozen_cash: Decimal = Decimal("0")
    frozen_commission: Decimal
    frozen_position_volume: int
    margin_rule_id: int | None = None
    margin_rule_version: str | None = None
    margin_price_mode: str | None = None
    margin_underlying_price: Decimal | None = None
    margin_option_price: Decimal | None = None
    margin_calculation_version: str | None = None
    margin_risk_state: str = "NORMAL"
    order_source: str = "USER"
    liquidation_task_id: str | None = None
    reduce_only: bool = False
    fee_rule_id: int | None = None
    fee_rule_version: str | None = None

    # 订单状态及可能的拒绝原因
    status: OrderStatus
    submit_status: OrderSubmitStatus
    reject_code: str | None
    reject_message: str | None
    cancel_reason_code: str | None = None
    cancel_reason_message: str | None = None

    # 生命周期时间
    created_at: datetime
    accepted_at: datetime | None
    cancelled_at: datetime | None
    updated_at: datetime

    @model_validator(mode="after")
    def populate_legacy_resolved_price(self):
        if self.resolved_price is None:
            self.resolved_price = self.limit_price
        return self


class OrderPageResponse(BaseModel):
    """订单不透明游标分页响应；items按数据库主键倒序稳定排列。"""

    items: list[OrderResponse]
    next_cursor: str | None
    has_more: bool
