from collections.abc import Callable

from app.matching.base import MatchingEngine
from app.matching.engines.vn import VnMatchingEngine


class MatchingEngineRegistry:
    """
    按配置名称注册并创建撮合引擎。

    名称统一去除首尾空白并转换为大写，避免环境变量大小写差异导致
    配置行为不一致。未知名称直接抛错，让 Worker 在启动阶段明确失败。
    """

    def __init__(self) -> None:
        self._factories: dict[str, Callable[[], MatchingEngine]] = {}

    @staticmethod
    def normalize_name(name: str) -> str:
        """生成稳定的配置查找键。"""

        return str(name).strip().upper()

    def register(
        self,
        name: str,
        factory: Callable[[], MatchingEngine],
    ) -> None:
        """注册一个无参数引擎工厂。"""

        normalized_name = self.normalize_name(name)
        if not normalized_name:
            raise ValueError("撮合引擎名称不能为空")
        self._factories[normalized_name] = factory

    def create(self, name: str) -> MatchingEngine:
        """根据配置创建引擎；未知名称必须阻止 Worker 启动。"""

        normalized_name = self.normalize_name(name)
        factory = self._factories.get(normalized_name)
        if factory is None:
            available = ", ".join(sorted(self._factories)) or "<none>"
            raise ValueError(
                f"未知撮合引擎名称: {name!r}，可用引擎: {available}"
            )
        engine = factory()
        if not isinstance(engine, MatchingEngine):
            raise TypeError(
                f"撮合引擎 {normalized_name} 未实现 MatchingEngine 接口"
            )
        return engine


# 进程级注册器只保存工厂；真正的引擎实例由 Worker 启动时创建一次。
matching_engine_registry = MatchingEngineRegistry()
matching_engine_registry.register(VnMatchingEngine.name, VnMatchingEngine)


def create_matching_engine(name: str = "VN") -> MatchingEngine:
    """使用默认注册器创建配置指定的撮合引擎。"""

    return matching_engine_registry.create(name)
