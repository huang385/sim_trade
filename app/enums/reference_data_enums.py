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
