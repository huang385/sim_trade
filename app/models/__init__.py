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
from app.models.margin_rule import MarginRule
from app.models.margin_rule_daily import MarginRuleDaily
from app.models.fee_rule import FeeRule
from app.models.fee_rule_daily import FeeRuleDaily
from app.models.reference_sync_log import ReferenceSyncLog
from app.models.order import Order
from app.models.outbox_event import OutboxEvent
from app.models.trade import Trade
from app.models.position import Position
from app.models.position_detail import PositionDetail
from app.models.position_freeze_allocation import PositionFreezeAllocation


__all__ = [
    "Account",
    "Instrument",
    "MarginRule",
    "MarginRuleDaily",
    "FeeRule",
    "FeeRuleDaily",
    "ReferenceSyncLog",
    "Order",
    "OutboxEvent",
    "Trade",
    "Position",
    "PositionDetail",
    "PositionFreezeAllocation",
]
