from enum import Enum


class InstrumentType(str, Enum):
    """中性的合约类型，用于跨产品业务分派。"""

    FUTURES = "FUTURES"
    FUTURES_OPTION = "FUTURES_OPTION"
    INDEX = "INDEX"
    INDEX_OPTION = "INDEX_OPTION"
    STOCK = "STOCK"
