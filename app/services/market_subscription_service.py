from dataclasses import dataclass

from app.common.code_utils import normalize_code
from app.infrastructure.active_order_index import ActiveOrderIndex


@dataclass(frozen=True)
class SubscriptionChange:
    """防抖完成后需要应用的新订阅集合。"""

    codes: frozenset[str]


class MarketSubscriptionService:
    """从Redis活动订单派生目标合约，并管理订阅集合防抖状态。"""

    def __init__(
        self,
        *,
        active_order_index: ActiveOrderIndex,
        debounce_seconds: float,
    ):
        self.active_order_index = active_order_index
        self.debounce_seconds = debounce_seconds
        self.current_codes: frozenset[str] = frozenset()
        self._pending_codes: frozenset[str] | None = None
        self._pending_since: float | None = None

    def get_desired_codes(self) -> frozenset[str]:
        """只读取active_orders:all及详情Hash，去重提取order_book_id。"""

        desired: set[str] = set()
        for order_id in self.active_order_index.list_all_order_ids():
            detail = self.active_order_index.get_active_order(order_id)
            raw_code = detail.get("order_book_id")
            if not raw_code:
                continue
            try:
                desired.add(normalize_code(raw_code))
            except ValueError:
                continue
        return frozenset(desired)

    def observe(
        self,
        desired_codes: frozenset[str],
        *,
        now: float,
    ) -> SubscriptionChange | None:
        """目标集合连续稳定达到防抖时间后才返回变更。"""

        if desired_codes == self.current_codes:
            self._pending_codes = None
            self._pending_since = None
            return None
        if desired_codes != self._pending_codes:
            self._pending_codes = desired_codes
            self._pending_since = now
            return None
        if self._pending_since is None:
            self._pending_since = now
            return None
        if now - self._pending_since < self.debounce_seconds:
            return None
        return SubscriptionChange(desired_codes)

    def mark_applied(self, codes: frozenset[str]) -> None:
        """仅在停止完成或新订阅成功建立后更新当前集合。"""

        self.current_codes = codes
        self._pending_codes = None
        self._pending_since = None
