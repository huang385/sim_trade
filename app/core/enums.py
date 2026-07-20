from enum import Enum


class AccountType(str, Enum):
    """
    账户类型
    """
    FUTURES = "FUTURES"      # 期货账户
    STOCK = "STOCK"          # 股票账户
    OPTION = "OPTION"        # 期权账户
    CRYPTO = "CRYPTO"        # 数字货币账户


class AccountStatus(str, Enum):
    """
    账户状态
    """
    NORMAL = "NORMAL"              # 正常
    DISABLED = "DISABLED"          # 禁用
    LIQUIDATION = "LIQUIDATION"    # 强平中


class MarketType(str, Enum):
    """
    市场类型
    """
    FUTURES = "FUTURES"      # 期货
    STOCK = "STOCK"          # 股票
    OPTION = "OPTION"        # 期权
    CRYPTO = "CRYPTO"        # 数字货币


class FeeMode(str, Enum):
    """
    手续费计算方式
    """
    BY_VOLUME = "BY_VOLUME"  # 按手数收费，例如 3元/手
    BY_AMOUNT = "BY_AMOUNT"  # 按成交金额比例收费，例如 万分之一