from dataclasses import dataclass
from decimal import Decimal

from app.enums.account_enums import AccountRiskState


@dataclass(frozen=True)
class RiskDecision:
    """纯Decimal风险判断结果，不依赖数据库、Redis或HTTP。"""

    state: str
    reason: str


class AccountRiskStateService:
    """统一账户风险状态机和阈值迟滞规则。"""

    OPEN_BLOCKED_STATES = frozenset(
        {
            AccountRiskState.MARGIN_DEFICIT.value,
            AccountRiskState.LIQUIDATION_PENDING.value,
            AccountRiskState.LIQUIDATING.value,
            AccountRiskState.VALUATION_UNAVAILABLE.value,
        }
    )

    @staticmethod
    def validate_thresholds(
        *, warning_ratio: Decimal, liquidation_ratio: Decimal, recovery_ratio: Decimal
    ) -> None:
        if any(
            not isinstance(value, Decimal)
            for value in (warning_ratio, liquidation_ratio, recovery_ratio)
        ):
            raise TypeError("风险阈值必须使用Decimal")
        if not Decimal("0") <= recovery_ratio < warning_ratio < liquidation_ratio:
            raise ValueError("风险阈值必须满足0 <= recovery < warning < liquidation")

    @classmethod
    def evaluate(
        cls,
        *,
        current_state: str,
        valuation_available: bool,
        equity: Decimal,
        risk_available_cash: Decimal,
        risk_ratio: Decimal,
        warning_ratio: Decimal,
        liquidation_ratio: Decimal,
        recovery_ratio: Decimal,
    ) -> RiskDecision:
        """按可靠估值、资金缺口和迟滞阈值计算下一风险状态。"""

        cls.validate_thresholds(
            warning_ratio=warning_ratio,
            liquidation_ratio=liquidation_ratio,
            recovery_ratio=recovery_ratio,
        )
        values = (equity, risk_available_cash, risk_ratio)
        if any(not isinstance(value, Decimal) for value in values):
            raise TypeError("风险指标必须使用Decimal")
        if not valuation_available:
            return RiskDecision(
                AccountRiskState.VALUATION_UNAVAILABLE.value,
                "VALUATION_UNAVAILABLE",
            )
        if (
            equity <= Decimal("0")
            or risk_available_cash < Decimal("0")
            or risk_ratio >= liquidation_ratio
        ):
            if current_state in {
                AccountRiskState.LIQUIDATION_PENDING.value,
                AccountRiskState.LIQUIDATING.value,
            }:
                return RiskDecision(current_state, "RISK_LIMIT_STILL_EXCEEDED")
            return RiskDecision(
                AccountRiskState.MARGIN_DEFICIT.value,
                "EQUITY_NON_POSITIVE"
                if equity <= Decimal("0")
                else "RISK_LIMIT_EXCEEDED",
            )
        # WARNING及强平状态只有降至更低的恢复阈值才解除，形成迟滞区间。
        was_abnormal = current_state != AccountRiskState.NORMAL.value
        if risk_ratio >= warning_ratio or (was_abnormal and risk_ratio > recovery_ratio):
            return RiskDecision(AccountRiskState.WARNING.value, "RISK_WARNING_RATIO")
        if current_state in {
            AccountRiskState.WARNING.value,
            AccountRiskState.MARGIN_DEFICIT.value,
            AccountRiskState.LIQUIDATION_PENDING.value,
            AccountRiskState.LIQUIDATING.value,
            AccountRiskState.VALUATION_UNAVAILABLE.value,
        }:
            return RiskDecision(AccountRiskState.RECOVERED.value, "RISK_RECOVERED")
        if current_state == AccountRiskState.RECOVERED.value:
            return RiskDecision(AccountRiskState.NORMAL.value, "RECOVERY_CONFIRMED")
        return RiskDecision(AccountRiskState.NORMAL.value, "RISK_NORMAL")

    @staticmethod
    def resolve_full_evaluation(
        *, valuation_unavailable: bool, margin_deficit: bool
    ) -> str:
        """兼容估值链路：局部估值只提升严重状态，不负责预警和强平编排。"""

        if valuation_unavailable:
            return AccountRiskState.VALUATION_UNAVAILABLE.value
        if margin_deficit:
            return AccountRiskState.MARGIN_DEFICIT.value
        return AccountRiskState.NORMAL.value

    @staticmethod
    def preserve_for_local_update(
        current_state: str,
        *,
        valuation_unavailable: bool = False,
        margin_deficit: bool = False,
    ) -> str:
        """局部更新不能把尚未完整复核的风险状态错误恢复为NORMAL。"""

        if valuation_unavailable or current_state == AccountRiskState.VALUATION_UNAVAILABLE.value:
            return AccountRiskState.VALUATION_UNAVAILABLE.value
        if margin_deficit or current_state in {
            AccountRiskState.MARGIN_DEFICIT.value,
            AccountRiskState.LIQUIDATION_PENDING.value,
            AccountRiskState.LIQUIDATING.value,
        }:
            return current_state if current_state != AccountRiskState.NORMAL.value else AccountRiskState.MARGIN_DEFICIT.value
        return current_state

    @classmethod
    def ensure_open_allowed(cls, risk_state: str) -> None:
        """所有产品的OPEN订单共用同一风险拦截边界。"""

        if risk_state in cls.OPEN_BLOCKED_STATES:
            from app.common.exceptions import BusinessRuleError

            raise BusinessRuleError(
                "账户当前风险状态禁止增加风险",
                error_code="ACCOUNT_RISK_OPEN_BLOCKED",
            )
