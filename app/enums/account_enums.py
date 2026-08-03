from enum import Enum


class AccountType(str, Enum):
    """交易账户类型。"""

    # 期货账户
    FUTURES = "FUTURES"

    # 股票账户
    STOCK = "STOCK"

    # 期权账户
    OPTION = "OPTION"

    # 数字货币账户
    CRYPTO = "CRYPTO"


class AccountStatus(str, Enum):
    """交易账户状态。"""

    # 正常状态，允许下单
    NORMAL = "NORMAL"

    # 已禁用，不允许下单
    DISABLED = "DISABLED"

    # 强平处理中，不允许提交普通订单
    LIQUIDATION = "LIQUIDATION"


class AccountRiskState(str, Enum):
    """实时估值产生的账户风险状态，不替代账户启停状态。"""

    NORMAL = "NORMAL"
    MARGIN_DEFICIT = "MARGIN_DEFICIT"
    VALUATION_UNAVAILABLE = "VALUATION_UNAVAILABLE"
