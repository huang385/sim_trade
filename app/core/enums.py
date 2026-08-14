"""核心枚举的兼容导入层。

枚举唯一事实来源位于 ``app.enums``。旧代码若仍从 ``app.core.enums``
导入，可以继续工作，但这里不再重复定义同名 Enum。
"""

from app.enums.account_enums import AccountStatus, AccountType
from app.enums.market_enums import MarketType
from app.enums.reference_data_enums import CommissionType


# 历史名称仅保留为同一个类对象的别名，禁止字符串隐式桥接两套枚举。
FeeMode = CommissionType

__all__ = ["AccountStatus", "AccountType", "FeeMode", "MarketType"]
