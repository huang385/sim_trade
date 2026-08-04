from collections import Counter
from threading import Lock


class RealtimeMetrics:
    """无外部依赖的进程内指标；后续可直接接入正式监控采集器。"""

    def __init__(self):
        self._values: Counter[str] = Counter()
        self._lock = Lock()

    def increment(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._values[name] += amount

    def set(self, name: str, value: int) -> None:
        with self._lock:
            self._values[name] = value

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._values)


realtime_metrics = RealtimeMetrics()
