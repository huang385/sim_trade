from enum import Enum


class RiskEventType(str, Enum):
    """账户风险审计事件类型。"""

    WARNING = "RISK_WARNING"
    STATE_CHANGED = "RISK_STATE_CHANGED"
    LIQUIDATION_STARTED = "LIQUIDATION_STARTED"
    LIQUIDATION_ORDER_UPDATED = "LIQUIDATION_ORDER_UPDATED"
    LIQUIDATION_COMPLETED = "LIQUIDATION_COMPLETED"
    LIQUIDATION_FAILED = "LIQUIDATION_FAILED"


class LiquidationTaskStatus(str, Enum):
    """强平任务的可恢复生命周期。"""

    PENDING = "PENDING"
    LIQUIDATING = "LIQUIDATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class OrderSource(str, Enum):
    """订单来源；用户订单与系统强平订单必须可审计地区分。"""

    USER = "USER"
    LIQUIDATION = "LIQUIDATION"
