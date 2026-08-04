from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from app.schemas.account_schema import AccountResponse
from app.schemas.position_schema import PositionResponse


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
    instrument_type: str = "FUTURES"
    option_market_value: Decimal = Decimal("0")
    realtime_required_margin: Decimal = Decimal("0")
    event_time: datetime
    source_event_id: str
    updated_at: datetime
    data_source: str = "REDIS_REALTIME"
    # 由Redis Lua在成功写入周期内设置；Python资金计算不参与版本生成。
    realtime_snapshot_version: str | None = None


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
    futures_unrealized_pnl: Decimal = Decimal("0")
    option_realtime_required_margin: Decimal = Decimal("0")
    long_option_market_value: Decimal = Decimal("0")
    short_option_market_value: Decimal = Decimal("0")
    net_option_market_value: Decimal = Decimal("0")
    risk_available_cash: Decimal = Decimal("0")
    risk_state: str = "NORMAL"
    risk_ratio: Decimal
    updated_at: datetime
    data_source: str = "REDIS_REALTIME"
    realtime_snapshot_version: str | None = None


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


class PositionTradingSnapshotResponse(BaseModel):
    """账户级快照中的单条持仓及其对应实时盈亏。"""

    position: PositionResponse
    pnl: PositionRealtimePnlResponse


class AccountTradingSnapshotResponse(BaseModel):
    """
    测试交易页面一次刷新所需的完整账户快照。

    账户和持仓事实来自PostgreSQL，盘中资金与盈亏优先来自Redis；Redis缺失时
    使用同一批已查询的数据库对象回退，不会再次逐持仓查询数据库。
    """

    account: AccountResponse
    pnl: AccountRealtimePnlResponse
    positions: list[PositionTradingSnapshotResponse]
