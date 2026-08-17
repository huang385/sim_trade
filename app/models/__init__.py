"""
统一导入所有 SQLAlchemy 数据库模型。

作用：

当程序执行：

    Base.metadata.create_all(bind=engine)

SQLAlchemy 必须提前加载所有模型类，
否则它不知道需要创建哪些数据库表。
"""

from app.models.account import Account
from app.models.instrument import Instrument
from app.models.instrument_market_data_mapping import (
    InstrumentMarketDataMapping,
)
from app.models.margin_rule import MarginRule
from app.models.margin_rule_daily import MarginRuleDaily
from app.models.fee_rule import FeeRule
from app.models.fee_rule_daily import FeeRuleDaily
from app.models.fee_rule_item import FeeRuleItem
from app.models.option_margin_rule import OptionMarginRule
from app.models.reference_sync_log import ReferenceSyncLog
from app.models.order import Order
from app.models.outbox_event import OutboxEvent
from app.models.trade import Trade
from app.models.position import Position
from app.models.position_detail import PositionDetail
from app.models.position_freeze_allocation import PositionFreezeAllocation
from app.models.trade_position_allocation import TradePositionAllocation
from app.models.app_user import AppUser
from app.models.auth_refresh_session import AuthRefreshSession
from app.models.liquidation_task import LiquidationTask
from app.models.risk_event import RiskEvent
from app.models.daily_settlement import (
    DailyAccountSettlement,
    DailyPositionSettlement,
    DailySettlementBatch,
    InstrumentSettlementPrice,
    OptionExpirySettlementDetail,
)
from app.models.stock_daily_trading_fact import StockDailyTradingFact
from app.models.stock_trading_rule import StockTradingRule


__all__ = [
    "Account",
    "Instrument",
    "InstrumentMarketDataMapping",
    "MarginRule",
    "MarginRuleDaily",
    "FeeRule",
    "FeeRuleDaily",
    "FeeRuleItem",
    "OptionMarginRule",
    "ReferenceSyncLog",
    "Order",
    "OutboxEvent",
    "Trade",
    "Position",
    "PositionDetail",
    "PositionFreezeAllocation",
    "TradePositionAllocation",
    "AppUser",
    "AuthRefreshSession",
    "LiquidationTask",
    "RiskEvent",
    "DailySettlementBatch",
    "InstrumentSettlementPrice",
    "DailyAccountSettlement",
    "DailyPositionSettlement",
    "OptionExpirySettlementDetail",
    "StockDailyTradingFact",
    "StockTradingRule",
]
