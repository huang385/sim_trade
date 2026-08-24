from dataclasses import dataclass
from threading import RLock
from typing import Any, Protocol

from app.common.code_utils import normalize_code
from app.infrastructure.active_order_index import ActiveOrderIndex


class ActivePositionContractSource(Protocol):
    """行情订阅服务所需的最小活动持仓合约读取接口。"""

    def list_active_contract_codes(self) -> set[str]:
        """返回当前至少存在一条有效持仓的合约代码。"""

    def list_margin_dependency_codes(self) -> set[str]:
        """返回活动期权空头持仓依赖的标的订阅代码。"""


class PreSubscriptionContractSource(Protocol):
    """下单前临时行情需求所需的最小读取接口。"""

    def list_active_contract_codes(self) -> set[str]:
        """返回所有尚未到期的期权及标的标准代码。"""


@dataclass(frozen=True)
class SubscriptionChange:
    """防抖完成后需要应用的新订阅集合。"""

    codes: frozenset[str]


@dataclass(frozen=True)
class SubscriptionStateSnapshot:
    """异步订阅回执状态的线程安全只读快照。"""

    generation: int
    requested_codes: frozenset[str]
    subscribed_codes: frozenset[str]
    failed_codes: frozenset[str]
    failure_reasons: dict[str, str]

    @property
    def all_subscribed(self) -> bool:
        return bool(self.requested_codes) and (
            self.subscribed_codes == self.requested_codes
        )


class MarketSubscriptionService:
    """发现目标合约，并维护请求、成功和失败三组订阅状态。"""

    def __init__(
        self,
        *,
        active_order_index: ActiveOrderIndex,
        active_position_contract_source: ActivePositionContractSource,
        debounce_seconds: float,
        pre_subscription_source: PreSubscriptionContractSource | None = None,
        cash_security_position_source: ActivePositionContractSource | None = None,
    ):
        self.active_order_index = active_order_index
        self.active_position_contract_source = (
            active_position_contract_source
        )
        self.cash_security_position_source = cash_security_position_source
        self.debounce_seconds = debounce_seconds
        self.pre_subscription_source = pre_subscription_source
        self._requested_codes: frozenset[str] = frozenset()
        self._subscribed_codes: set[str] = set()
        self._failure_reasons: dict[str, str] = {}
        self._generation = 0
        self._pending_codes: frozenset[str] | None = None
        self._pending_since: float | None = None
        self._lock = RLock()

    def _get_active_order_codes(self) -> set[str]:
        """从独立活动合约Set读取仍需撮合的订单合约。"""

        desired: set[str] = set()
        for raw_code in self.active_order_index.list_active_contract_codes():
            try:
                desired.add(normalize_code(raw_code))
            except ValueError:
                continue
        return desired

    def _get_active_position_codes(self) -> set[str]:
        """从Redis持仓合约索引读取仍需盯市的有效持仓合约。"""

        desired: set[str] = set()
        for source in (
            self.active_position_contract_source,
            self.cash_security_position_source,
        ):
            if source is None:
                continue
            for raw_code in source.list_active_contract_codes():
                try:
                    desired.add(normalize_code(raw_code))
                except ValueError:
                    continue
        return desired

    def _get_margin_dependency_codes(self) -> set[str]:
        """汇总活动期权卖方订单和空头持仓依赖的标的代码。"""

        desired: set[str] = set()
        for source in (
            self.active_order_index,
            self.active_position_contract_source,
            self.cash_security_position_source,
        ):
            loader = getattr(source, "list_margin_dependency_codes", None)
            if loader is None:
                continue
            raw_codes = loader()
            # 兼容只实现旧协议的测试替身和渐进升级调用方；生产实现固定
            # 返回集合，未知对象不能被当作订阅代码迭代。
            if not isinstance(raw_codes, (set, frozenset, list, tuple)):
                continue
            for raw_code in raw_codes:
                try:
                    desired.add(normalize_code(raw_code))
                except ValueError:
                    continue
        return desired

    def _get_pre_subscription_codes(self) -> set[str]:
        """读取仍有效的临时期权行情需求；过期成员由Redis读取时清理。"""

        if self.pre_subscription_source is None:
            return set()
        desired: set[str] = set()
        for raw_code in (
            self.pre_subscription_source.list_active_contract_codes()
        ):
            try:
                desired.add(normalize_code(raw_code))
            except ValueError:
                continue
        return desired

    def get_desired_codes(self) -> frozenset[str]:
        """
        汇总目标订阅集合。

        活动订单需要行情进行撮合；有效持仓需要行情持续计算实时盈亏。
        两者取并集并自动去重。只有某合约既无活动订单、也无有效持仓时，
        才会在既有防抖流程结束后从订阅集合移除。
        """

        return frozenset(
            self._get_active_order_codes()
            | self._get_active_position_codes()
            | self._get_margin_dependency_codes()
            | self._get_pre_subscription_codes()
        )

    def observe(
        self,
        desired_codes: frozenset[str],
        *,
        now: float,
    ) -> SubscriptionChange | None:
        """新增订阅立即生效；移除订阅仍需连续稳定达到防抖时间。"""

        with self._lock:
            if desired_codes == self._requested_codes:
                self._pending_codes = None
                self._pending_since = None
                return None
            added_codes = desired_codes - self._requested_codes
            if added_codes:
                # 新合约需要尽快拿到首条行情。若本轮同时存在删除，先保留旧
                # 合约并立即追加新合约；下一轮再单独对删除集合执行防抖。
                self._pending_codes = None
                self._pending_since = None
                return SubscriptionChange(
                    frozenset(self._requested_codes | added_codes)
                )
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

    def mark_requested(self, codes: frozenset[str]) -> int:
        """发起一次新订阅，只登记 requested_codes，等待异步回执确认。"""

        with self._lock:
            self._generation += 1
            self._requested_codes = frozenset(codes)
            self._subscribed_codes.clear()
            self._failure_reasons.clear()
            self._pending_codes = None
            self._pending_since = None
            return self._generation

    def mark_applied(self, codes: frozenset[str]) -> None:
        """兼容旧调用；新代码应使用 mark_requested，并等待订阅回执。"""

        self.mark_requested(codes)

    def apply_subscription_report(
        self,
        report: dict[str, Any],
        *,
        generation: int | None = None,
    ) -> SubscriptionStateSnapshot:
        """
        幂等合并逐合约回执。

        同一代订阅中成功是单调状态：成功后到达的重复失败回执不会把合约
        移入 failed_codes；先失败后成功则由成功回执清除失败原因。
        """

        contracts = report.get("contracts") or {}
        with self._lock:
            if generation is not None and generation != self._generation:
                return self._snapshot_unlocked()

            for raw_code, item in contracts.items():
                try:
                    code = normalize_code(raw_code)
                except ValueError:
                    continue
                if code not in self._requested_codes:
                    continue

                succeeded = bool(
                    item.get("exists", False)
                    and item.get("is_live", False)
                    and item.get("subscribed", False)
                )
                if succeeded:
                    self._subscribed_codes.add(code)
                    self._failure_reasons.pop(code, None)
                    continue

                # 成功优先，保证重复或乱序回执不会破坏最终状态。
                if code in self._subscribed_codes:
                    continue
                if not item.get("exists", False):
                    reason = "CONTRACT_NOT_FOUND"
                elif not item.get("is_live", False):
                    reason = "CONTRACT_NOT_LIVE"
                else:
                    reason = "SUBSCRIBE_FAILED"
                self._failure_reasons[code] = reason

            return self._snapshot_unlocked()

    def clear(self) -> None:
        """清空目标及回执状态，使状态机进入 IDLE。"""

        self.mark_requested(frozenset())

    def _snapshot_unlocked(self) -> SubscriptionStateSnapshot:
        return SubscriptionStateSnapshot(
            generation=self._generation,
            requested_codes=self._requested_codes,
            subscribed_codes=frozenset(self._subscribed_codes),
            failed_codes=frozenset(self._failure_reasons),
            failure_reasons=dict(self._failure_reasons),
        )

    def state_snapshot(self) -> SubscriptionStateSnapshot:
        with self._lock:
            return self._snapshot_unlocked()

    @property
    def current_codes(self) -> frozenset[str]:
        """兼容旧代码：当前集合指已发起请求的目标集合，不代表全部成功。"""

        return self.state_snapshot().requested_codes
