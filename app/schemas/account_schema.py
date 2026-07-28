from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.enums.account_enums import AccountType


class AccountCreate(BaseModel):
    """
    创建模拟交易账户的请求参数。
    """

    # 账户编号，例如 092001
    account_id: str = Field(min_length=1, max_length=64)

    # 用户编号，一个用户可以拥有多个账户
    user_id: str | None = Field(default=None, max_length=64)

    # 账户名称
    account_name: str = Field(min_length=1, max_length=128)

    # 账户类型，第一版默认期货账户
    account_type: AccountType = AccountType.FUTURES

    # 初始资金，必须大于 0
    initial_cash: Decimal = Field(gt=0)


class AccountResponse(BaseModel):
    """
    模拟账户返回结构。
    """

    # 允许直接读取 SQLAlchemy ORM 对象
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: str
    user_id: str | None
    account_name: str
    account_type: str

    initial_cash: Decimal
    cash_balance: Decimal
    available_cash: Decimal
    frozen_cash: Decimal

    equity: Decimal

    used_margin: Decimal
    frozen_margin: Decimal

    realized_pnl: Decimal
    unrealized_pnl: Decimal
    daily_position_pnl: Decimal
    daily_close_pnl: Decimal
    daily_commission: Decimal
    daily_pnl: Decimal

    used_commission: Decimal
    frozen_commission: Decimal

    risk_ratio: Decimal
    status: str
    trading_day: date | None

    created_at: datetime
    updated_at: datetime
