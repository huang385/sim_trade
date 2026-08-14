from enum import Enum


class OptionType(str, Enum):
    """期权权利类型。"""

    CALL = "CALL"
    PUT = "PUT"


class ExerciseStyle(str, Enum):
    """期权行权方式；本阶段只保存，不执行行权。"""

    AMERICAN = "AMERICAN"
    EUROPEAN = "EUROPEAN"


class SettlementType(str, Enum):
    """期权到期结算方式；本阶段只保存，不执行到期结算。"""

    PHYSICAL = "PHYSICAL"
    CASH = "CASH"


class MarginPriceMode(str, Enum):
    """期权保证金采用的价格口径。"""

    ORDER_FREEZE = "ORDER_FREEZE"
    REALTIME = "REALTIME"
    SETTLEMENT = "SETTLEMENT"


class OptionMarginAlgorithm(str, Enum):
    """期权卖方保证金算法类型。"""

    COMMODITY_FUTURES_OPTION = "COMMODITY_FUTURES_OPTION"
    CFFEX_INDEX_OPTION = "CFFEX_INDEX_OPTION"
