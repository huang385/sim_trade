from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

from app.enums.risk_enums import RiskEventType
from app.services.risk_event_service import RiskEventService


def account():
    return SimpleNamespace(
        account_id="A001",
        risk_state="WARNING",
        risk_version=4,
        equity=Decimal("100000"),
        available_cash=Decimal("50000"),
        risk_available_cash=Decimal("40000"),
        used_margin=Decimal("50000"),
        option_realtime_required_margin=Decimal("10000"),
        frozen_margin=Decimal("0"),
        frozen_cash=Decimal("0"),
        frozen_commission=Decimal("5"),
        risk_ratio=Decimal("0.80000000"),
    )


def test_risk_event_and_outbox_are_created_with_same_monotonic_version():
    risk_repository = Mock()
    outbox_repository = Mock()
    service = RiskEventService(
        risk_repository=risk_repository,
        outbox_repository=outbox_repository,
    )
    item = account()
    now = datetime.now(timezone.utc)

    event = service.record(
        Mock(),
        account=item,
        event_type=RiskEventType.WARNING,
        previous_state="NORMAL",
        trigger_reason="RISK_WARNING_RATIO",
        occurred_at=now,
    )

    assert item.risk_version == 5
    assert event.business_version == 5
    assert event.snapshot["equity"] == "100000"
    payload = outbox_repository.create_event.call_args.kwargs["payload"]
    assert payload["risk_version"] == 5
    assert payload["business_version"] == 5
    assert payload["risk_ratio"] == "0.80000000"
    assert isinstance(payload["risk_available_cash"], str)


def test_liquidation_event_contains_task_and_order_trace_fields():
    outbox_repository = Mock()
    service = RiskEventService(
        risk_repository=Mock(), outbox_repository=outbox_repository
    )
    service.record(
        Mock(),
        account=account(),
        event_type=RiskEventType.LIQUIDATION_ORDER_UPDATED,
        previous_state="LIQUIDATING",
        trigger_reason="LIQUIDATION_ORDER_CREATED",
        occurred_at=datetime.now(timezone.utc),
        extra={"task_id": "T1", "order_id": "O1"},
    )
    payload = outbox_repository.create_event.call_args.kwargs["payload"]
    assert payload["task_id"] == "T1"
    assert payload["order_id"] == "O1"
