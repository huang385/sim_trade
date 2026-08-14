from enum import Enum


class ProductFamily(str, Enum):
    """稳定的产品族标识；具体交易规则仍属于各产品模块。"""

    FUTURES = "FUTURES"
    OPTIONS = "OPTIONS"
