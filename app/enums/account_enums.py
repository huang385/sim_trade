from enum import Enum


class AccountType(str, Enum):
    """交易账户类型。"""

    # 期货账户
    FUTURES = "FUTURES"

    # 股票账户
    STOCK = "STOCK"

    # 现金证券账户的标准类型。为兼容现金证券边界引入前创建的历史记录，
    # 仍保留 STOCK 类型的可读与处理能力。
    SECURITIES_CASH = "SECURITIES_CASH"

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
    WARNING = "WARNING"
    MARGIN_DEFICIT = "MARGIN_DEFICIT"
    LIQUIDATION_PENDING = "LIQUIDATION_PENDING"
    LIQUIDATING = "LIQUIDATING"
    RECOVERED = "RECOVERED"
    VALUATION_UNAVAILABLE = "VALUATION_UNAVAILABLE"
