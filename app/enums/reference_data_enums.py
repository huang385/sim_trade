from enum import Enum


class CommissionType(str, Enum):
    """手续费计算类型。"""

    # 按成交手数收费
    BY_VOLUME = "BY_VOLUME"

    # 按成交金额比例收费
    BY_AMOUNT = "BY_AMOUNT"


class ReferenceDataSource(str, Enum):
    """交易参考数据来源。"""

    # RQData 自动同步
    RQDATA = "RQDATA"

    # 管理员人工录入
    MANUAL = "MANUAL"

    # 自有数据系统同步
    INTERNAL = "INTERNAL"


class StockPriceLimitType(str, Enum):
    """股票逐日涨跌停规则的表达方式。"""

    RATIO = "RATIO"
    NONE = "NONE"


class StockDailyTradingFactUpsertResult(str, Enum):
    """逐日事实同步写入的确定性结果。"""

    INSERTED = "INSERTED"
    UPDATED = "UPDATED"
    DUPLICATE = "DUPLICATE"
    IGNORED_STALE = "IGNORED_STALE"
    CONFLICT_SAME_TIMESTAMP = "CONFLICT_SAME_TIMESTAMP"


class FeeType(str, Enum):
    """可组合的订单手续费类型。"""

    DERIVATIVE_COMMISSION = "DERIVATIVE_COMMISSION"
    BROKER_COMMISSION = "BROKER_COMMISSION"
    STAMP_DUTY = "STAMP_DUTY"
    TRANSFER_FEE = "TRANSFER_FEE"
    HANDLING_FEE = "HANDLING_FEE"
    OTHER = "OTHER"


class FeeAggregationScope(str, Enum):
    """最低收费的累计范围。"""

    ORDER = "ORDER"
    TRADE = "TRADE"
