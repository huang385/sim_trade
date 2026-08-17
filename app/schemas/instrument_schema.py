from datetime import date, datetime
from decimal import Decimal
from app.enums.instrument_enums import InstrumentType
from app.enums.market_enums import MarketType
from app.enums.option_enums import (
    ExerciseStyle,
    OptionType,
    SettlementType,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator


class InstrumentCreate(BaseModel):
    """
    创建或人工补录合约的请求参数。

    正常情况下，合约信息主要由 RQData 同步；
    该结构主要用于开发测试和人工补录。
    """

    # RQData标准合约代码，例如 RB2610
    order_book_id: str = Field(min_length=1, max_length=64)

    # 系统内部合约代码
    symbol: str = Field(min_length=1, max_length=64)

    # 交易所代码，例如 SHFE
    exchange_id: str = Field(min_length=1, max_length=32)

    # 合约中文名称
    instrument_name: str | None = Field(default=None, max_length=128)

    # 品种代码，例如 RB、CU
    product_id: str | None = Field(default=None, max_length=64)

    # 市场类型，第一版固定期货
    market_type: MarketType = MarketType.FUTURES
    instrument_type: InstrumentType = InstrumentType.FUTURES
    underlying_instrument_id: int | None = Field(default=None, gt=0)
    option_type: OptionType | None = None
    strike_price: Decimal | None = Field(default=None, gt=0)
    exercise_style: ExerciseStyle | None = None
    settlement_type: SettlementType | None = None
    # 合约乘数，必须大于 0
    contract_multiplier: Decimal = Field(gt=0)

    # 最小变动价位，必须大于 0
    price_tick: Decimal = Field(gt=0)

    # 最小下单量
    min_volume: int = Field(default=1, gt=0)

    # 最大下单量
    max_volume: int = Field(default=1_000_000, gt=0)

    # 上市日期
    listed_date: date | None = None

    # 到期日期
    expire_date: date | None = None
    last_trading_date: date | None = None

    # 是否允许交易
    is_active: bool = True
    is_tradeable: bool = True

    # 数据来源
    data_source: str = "MANUAL"

    @model_validator(mode="after")
    def validate_stock_fields(self):
        if self.instrument_type != InstrumentType.STOCK:
            return self
        if self.market_type != MarketType.STOCK:
            raise ValueError("股票 Instrument 的 market_type 必须为 STOCK")
        if self.contract_multiplier != Decimal("1"):
            raise ValueError("股票 Instrument 的 contract_multiplier 必须为 1")
        if any(
            value is not None
            for value in (
                self.underlying_instrument_id,
                self.option_type,
                self.strike_price,
                self.exercise_style,
                self.settlement_type,
            )
        ):
            raise ValueError("股票 Instrument 不能填写期权字段")
        return self


class InstrumentResponse(BaseModel):
    """
    合约信息返回结构。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    order_book_id: str
    symbol: str
    exchange_id: str

    instrument_name: str | None
    product_id: str | None
    market_type: MarketType = MarketType.FUTURES
    instrument_type: InstrumentType = InstrumentType.FUTURES
    underlying_instrument_id: int | None
    option_type: OptionType | None
    strike_price: Decimal | None
    exercise_style: ExerciseStyle | None
    settlement_type: SettlementType | None

    contract_multiplier: Decimal
    price_tick: Decimal

    min_volume: int
    max_volume: int

    listed_date: date | None
    expire_date: date | None
    last_trading_date: date | None
    is_active: bool
    is_tradeable: bool

    data_source: str
    synced_at: datetime | None
    created_at: datetime
    updated_at: datetime


class InstrumentCatalogItem(BaseModel):
    """桌面交易端可选择的有效期货合约。"""

    model_config = ConfigDict(from_attributes=True)

    order_book_id: str
    symbol: str
    exchange_id: str
    instrument_name: str | None
    product_id: str | None = None
    instrument_type: InstrumentType = InstrumentType.FUTURES
    underlying_order_book_id: str | None = None
    option_type: OptionType | None = None
    strike_price: Decimal | None = None
    expire_date: date | None = None
    contract_multiplier: Decimal | None = None
    price_tick: Decimal | None = None
