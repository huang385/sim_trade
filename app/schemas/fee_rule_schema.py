from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.enums.reference_data_enums import CommissionType


class FeeRuleBase(BaseModel):
    """
    手续费规则公共字段。
    """

    # RQData标准合约代码
    order_book_id: str = Field(min_length=1, max_length=64)

    # 系统内部合约代码
    symbol: str = Field(min_length=1, max_length=64)

    # 交易所代码
    exchange_id: str = Field(min_length=1, max_length=32)

    # 规则所属交易日
    trading_day: date

    # 手续费模式：
    # BY_VOLUME 按成交手数
    # BY_AMOUNT 按成交金额比例
    commission_type: CommissionType

    # 开仓手续费参数
    open_commission: Decimal = Field(default=Decimal("0"), ge=0)

    # 普通平仓手续费参数
    close_commission: Decimal = Field(default=Decimal("0"), ge=0)

    # 平今手续费参数
    close_today_commission: Decimal = Field(
        default=Decimal("0"),
        ge=0,
    )

    # 平今折扣率
    discount_rate: Decimal | None = Field(default=None, ge=0)

    # 数据来源
    data_source: str = "RQDATA"

    @model_validator(mode="after")
    def validate_commission_values(self):
        """
        校验手续费参数。

        手续费可以为 0，但不允许出现负数。
        commission_type 必须是系统支持的类型。
        """

        return self


class FeeRuleCreate(FeeRuleBase):
    """
    人工创建或覆盖当前手续费规则。
    """

    pass


class FeeRuleResponse(FeeRuleBase):
    """
    当前手续费规则返回结构。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    synced_at: datetime
    created_at: datetime
    updated_at: datetime


class FeeRuleDailyCreate(FeeRuleBase):
    """
    逐交易日手续费规则写入结构。

    主要由独立同步程序使用。
    """

    sync_batch_id: str | None = None


class FeeRuleDailyResponse(FeeRuleBase):
    """
    逐交易日手续费规则返回结构。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    sync_batch_id: str | None
    synced_at: datetime
    created_at: datetime
    updated_at: datetime
