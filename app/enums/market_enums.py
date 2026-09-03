from enum import Enum


class MarketType(str, Enum):
    """市场类型。"""

    FUTURES = "FUTURES"
    STOCK = "STOCK"
    BOND = "BOND"
    FUND = "FUND"
    OPTION = "OPTION"
    CRYPTO = "CRYPTO"


class ExchangeID(str, Enum):
    """国内期货交易所代码。"""

    # 上海期货交易所
    SHFE = "SHFE"

    # 大连商品交易所
    DCE = "DCE"

    # 郑州商品交易所
    CZCE = "CZCE"

    # 中国金融期货交易所
    CFFEX = "CFFEX"

    # 上海国际能源交易中心
    INE = "INE"

    # 广州期货交易所
    GFEX = "GFEX"
