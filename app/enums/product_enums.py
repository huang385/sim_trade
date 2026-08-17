from enum import Enum


class ProductFamily(str, Enum):
    """稳定的产品族标识；具体交易规则仍由产品策略负责。"""

    FUTURES = "FUTURES"
    OPTIONS = "OPTIONS"
    STOCKS = "STOCKS"
