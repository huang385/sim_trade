from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class TradeResponse(BaseModel):
    """
    成交查询响应。

    API只返回已经在PostgreSQL提交成功的成交。价格、成交额、保证金和
    手续费继续保持Decimal，不在响应Schema中转换成float。
    """

    model_config = ConfigDict(from_attributes=True)

    # 系统成交编号
    trade_id: str
    # 产生该成交的订单编号
    order_id: str
    # 成交所属账户
    account_id: str
    # 触发成交的行情事件编号
    market_event_id: str
    # 原始行情Stream消息编号
    market_stream_message_id: str
    # 标准合约编号
    order_book_id: str
    # 交易所和合约代码
    exchange_id: str
    symbol: str
    # 订单和成交所属交易日
    trading_day: date
    # 原订单买卖方向和开平标志
    direction: str
    offset_flag: str
    # 本次成交价格和手数
    trade_price: Decimal
    trade_volume: int
    # 成交额、保证金、手续费和已实现盈亏
    turnover: Decimal
    margin: Decimal
    commission: Decimal
    realized_pnl: Decimal
    # 行情事件时间和数据库写入时间
    trade_time: datetime
    created_at: datetime
