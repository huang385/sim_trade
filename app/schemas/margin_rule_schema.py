from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class MarginRuleBase(BaseModel):
    """
    保证金规则公共字段。
    """

    # RQData标准合约代码
    order_book_id: str = Field(min_length=1, max_length=64)

    # 系统内部合约代码
    symbol: str = Field(min_length=1, max_length=64)

    # 交易所代码
    exchange_id: str = Field(min_length=1, max_length=32)

    # 规则所属交易日
    trading_day: date

    # 多头保证金率，例如 0.12 表示 12%
    long_margin_rate: Decimal = Field(ge=0)

    # 空头保证金率
    short_margin_rate: Decimal = Field(ge=0)

    # 最低保证金率，可以为空
    min_margin_rate: Decimal | None = Field(default=None, ge=0)

    # 数据来源
    data_source: str = "RQDATA"


class MarginRuleCreate(MarginRuleBase):
    """
    人工创建或覆盖当前保证金规则。

    正常生产流程主要由同步程序写入，
    该结构用于开发测试和管理员补录。
    """

    pass


class MarginRuleResponse(MarginRuleBase):
    """
    当前保证金规则返回结构。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    synced_at: datetime
    created_at: datetime
    updated_at: datetime


class MarginRuleDailyCreate(MarginRuleBase):
    """
    逐交易日保证金规则写入结构。

    主要由独立 RQData 同步程序使用。
    """

    # 同步批次号
    sync_batch_id: str | None = None


class MarginRuleDailyResponse(MarginRuleBase):
    """
    逐交易日保证金规则返回结构。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    sync_batch_id: str | None
    synced_at: datetime
    created_at: datetime
    updated_at: datetime