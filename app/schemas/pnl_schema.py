from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class PositionRealtimePnl(BaseModel):
    """Redis中的单持仓盘中实时盈亏绝对快照。"""

    position_id: str
    account_id: str
    exchange_id: str
    symbol: str
    direction: str
    mark_price: Decimal
    cumulative_unrealized_pnl: Decimal
    daily_position_pnl: Decimal
    event_time: datetime
    source_event_id: str
    updated_at: datetime
    data_source: str = "REDIS_REALTIME"


class AccountRealtimePnl(BaseModel):
    """Redis中的账户盘中实时资金与盈亏绝对快照。"""

    account_id: str
    cumulative_unrealized_pnl: Decimal
    daily_position_pnl: Decimal
    daily_close_pnl: Decimal
    daily_commission: Decimal
    daily_pnl: Decimal
    equity: Decimal
    available_cash: Decimal
    risk_ratio: Decimal
    updated_at: datetime
    data_source: str = "REDIS_REALTIME"


class PositionRealtimePnlResponse(BaseModel):
    """持仓实时盈亏查询响应；Redis缺失时允许返回最近一次数据库快照。"""

    position_id: str
    account_id: str
    exchange_id: str
    symbol: str
    direction: str
    mark_price: Decimal | None
    unrealized_pnl: Decimal
    daily_position_pnl: Decimal
    event_time: datetime | None
    source_event_id: str | None
    updated_at: datetime
    data_source: str


class AccountRealtimePnlResponse(BaseModel):
    """账户实时盈亏查询响应，明确标记实时或持久化数据来源。"""

    account_id: str
    unrealized_pnl: Decimal
    daily_position_pnl: Decimal
    daily_close_pnl: Decimal
    daily_commission: Decimal
    daily_pnl: Decimal
    equity: Decimal
    available_cash: Decimal
    risk_ratio: Decimal
    updated_at: datetime
    data_source: str
