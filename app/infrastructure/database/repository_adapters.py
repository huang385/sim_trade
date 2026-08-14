"""Worker 装配使用的数据库适配器公共入口。

具体 Repository 仍保持单一实现；该入口把基础设施实现与 Worker 的模块路径解耦。
"""

from app.repositories.account_repository import AccountRepository
from app.repositories.instrument_market_data_mapping_repository import (
    InstrumentMarketDataMappingRepository,
)
from app.repositories.instrument_repository import InstrumentRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.outbox_repository import OutboxRepository
from app.repositories.risk_repository import RiskRepository

__all__ = [
    "AccountRepository",
    "InstrumentMarketDataMappingRepository",
    "InstrumentRepository",
    "OrderRepository",
    "OutboxRepository",
    "RiskRepository",
]
