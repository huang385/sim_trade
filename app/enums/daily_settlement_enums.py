from enum import Enum


class DailySettlementBatchStatus(str, Enum):
    """手工日终结算批次状态。"""

    RUNNING = "RUNNING"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"


class DailySettlementStage(str, Enum):
    """可恢复的结算阶段；阶段只在对应数据库事实提交后推进。"""

    PREFLIGHT = "PREFLIGHT"
    ORDERS_CANCELLED = "ORDERS_CANCELLED"
    BARRIER_CONFIRMED = "BARRIER_CONFIRMED"
    PRICES_FROZEN = "PRICES_FROZEN"
    ACCOUNTS_SETTLED = "ACCOUNTS_SETTLED"
    RECONCILED = "RECONCILED"
    COMPLETED = "COMPLETED"


class DailySettlementAccountStatus(str, Enum):
    """逐账户结算状态，支持崩溃后跳过已经完成的账户。"""

    RUNNING = "RUNNING"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"


class SettlementCacheStatus(str, Enum):
    """Redis 派生数据恢复状态，不改变 PostgreSQL 资金事实。"""

    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

