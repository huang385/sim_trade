"""
撮合领域的统一公开接口。

该包只保存纯撮合模型、引擎接口、引擎实现和创建注册器，不依赖
Redis、PostgreSQL、SQLAlchemy ORM 或成交结算服务。
"""

from app.matching.base import MatchingEngine
from app.matching.models import MatchResult, MatchingMarketData, MatchingOrder
from app.matching.registry import (
    MatchingEngineRegistry,
    create_matching_engine,
    matching_engine_registry,
)

__all__ = [
    "MatchingEngine",
    "MatchingOrder",
    "MatchingMarketData",
    "MatchResult",
    "MatchingEngineRegistry",
    "matching_engine_registry",
    "create_matching_engine",
]
