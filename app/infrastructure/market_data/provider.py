"""旧基础设施路径兼容层；端口定义已迁入 market_data 模块。"""

from app.modules.market_data.contracts import (
    MarketDataProvider,
    MarketDataSubscription,
)

__all__ = ["MarketDataProvider", "MarketDataSubscription"]
