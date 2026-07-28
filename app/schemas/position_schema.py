from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class PositionResponse(BaseModel):
    """
    持仓汇总查询响应。

    返回账户当前按合约和多空方向汇总后的结果，不展开PositionDetail逐笔
    明细。平仓成交后数量、成本、占用保证金和已实现盈亏同步更新。
    """

    model_config = ConfigDict(from_attributes=True)

    # 系统持仓编号和所属账户
    position_id: str
    account_id: str
    # 合约路由字段
    order_book_id: str
    exchange_id: str
    symbol: str
    # LONG或SHORT
    direction: str
    # 总量、今仓、昨仓、冻结量和可用量
    total_volume: int
    today_volume: int
    yesterday_volume: int
    frozen_volume: int
    available_volume: int
    # 加权平均开仓价、累计成本和实际占用保证金
    average_open_price: Decimal
    position_cost: Decimal
    used_margin: Decimal
    # 已实现盈亏盘中累计；未实现盈亏仍等待后续盯市阶段更新
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    daily_position_pnl: Decimal
    daily_close_pnl: Decimal
    # 交易日和生命周期时间
    trading_day: date
    created_at: datetime
    updated_at: datetime
