from decimal import Decimal
import pytest

from app.common.exceptions import BusinessRuleError
from app.enums.account_enums import AccountRiskState
from app.services.account_risk_state_service import AccountRiskStateService


def decide(*, state="NORMAL", ratio="0.10", equity="100000", cash="50000", available=True):
    return AccountRiskStateService.evaluate(
        current_state=state,
        valuation_available=available,
        equity=Decimal(equity),
        risk_available_cash=Decimal(cash),
        risk_ratio=Decimal(ratio),
        warning_ratio=Decimal("0.80"),
        liquidation_ratio=Decimal("1.00"),
        recovery_ratio=Decimal("0.75"),
    )


def test_normal_warning_and_margin_deficit_thresholds():
    assert decide(ratio="0.20").state == "NORMAL"
    assert decide(ratio="0.80").state == "WARNING"
    assert decide(ratio="1.00").state == "MARGIN_DEFICIT"
    assert decide(cash="-0.01").state == "MARGIN_DEFICIT"
    assert decide(equity="0").reason == "EQUITY_NON_POSITIVE"


def test_recovery_uses_lower_hysteresis_threshold():
    assert decide(state="WARNING", ratio="0.79").state == "WARNING"
    assert decide(state="WARNING", ratio="0.75").state == "RECOVERED"
    assert decide(state="RECOVERED", ratio="0.20").state == "NORMAL"


def test_valuation_unavailable_blocks_open_without_triggering_liquidation():
    result = decide(available=False, ratio="9")
    assert result.state == AccountRiskState.VALUATION_UNAVAILABLE.value
    with pytest.raises(BusinessRuleError) as exc_info:
        AccountRiskStateService.ensure_open_allowed(result.state)
    assert exc_info.value.error_code == "ACCOUNT_RISK_OPEN_BLOCKED"


@pytest.mark.parametrize(
    "state",
    ["MARGIN_DEFICIT", "LIQUIDATION_PENDING", "LIQUIDATING", "VALUATION_UNAVAILABLE"],
)
def test_all_risk_increasing_open_states_are_blocked(state):
    with pytest.raises(BusinessRuleError):
        AccountRiskStateService.ensure_open_allowed(state)


def test_thresholds_must_be_decimal_and_ordered():
    with pytest.raises(TypeError):
        AccountRiskStateService.validate_thresholds(
            warning_ratio=0.8,
            liquidation_ratio=Decimal("1"),
            recovery_ratio=Decimal("0.7"),
        )
    with pytest.raises(ValueError):
        AccountRiskStateService.validate_thresholds(
            warning_ratio=Decimal("0.8"),
            liquidation_ratio=Decimal("0.7"),
            recovery_ratio=Decimal("0.6"),
        )


def test_liquidating_state_is_preserved_while_deficit_continues():
    assert decide(state="LIQUIDATING", ratio="1.2").state == "LIQUIDATING"
