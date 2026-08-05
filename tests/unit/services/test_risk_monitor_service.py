from unittest.mock import Mock

from app.services.risk_monitor_service import RiskMonitorService


def service():
    return RiskMonitorService(
        session_factory=Mock(),
        cancellation_service=Mock(),
        account_repository=Mock(),
        order_repository=Mock(),
        risk_repository=Mock(),
        event_service=Mock(),
    )


def test_margin_deficit_cancels_open_orders_before_creating_task():
    item = service()
    calls = []
    item._evaluate_and_commit = Mock(
        side_effect=[
            ("MARGIN_DEFICIT", True, "RISK_LIMIT_EXCEEDED"),
            ("MARGIN_DEFICIT", False, "RISK_LIMIT_EXCEEDED"),
        ]
    )
    item._cancel_open_orders = Mock(side_effect=lambda account_id: calls.append("cancel") or 2)
    item._create_task = Mock(side_effect=lambda account_id, reason: calls.append("task") or "T1")
    item.revaluation_service = Mock()

    result = item.process_account("A001")

    assert calls == ["cancel", "task"]
    item.revaluation_service.recalculate_account.assert_called_once_with("A001")
    assert result.open_orders_cancelled == 2
    assert result.liquidation_task_id == "T1"
    assert result.retain_dirty is True


def test_cancellation_recovery_does_not_create_liquidation_task():
    item = service()
    item._evaluate_and_commit = Mock(
        side_effect=[
            ("MARGIN_DEFICIT", True, "RISK_LIMIT_EXCEEDED"),
            ("RECOVERED", True, "RISK_RECOVERED"),
        ]
    )
    item._cancel_open_orders = Mock(return_value=1)
    item._create_task = Mock()

    result = item.process_account("A001")

    assert result.state == "RECOVERED"
    item._create_task.assert_not_called()


def test_valuation_unavailable_retains_dirty_and_never_liquidates():
    item = service()
    item._evaluate_and_commit = Mock(
        return_value=("VALUATION_UNAVAILABLE", True, "VALUATION_UNAVAILABLE")
    )
    item._cancel_open_orders = Mock()
    item._create_task = Mock()

    result = item.process_account("A001")

    assert result.retain_dirty is True
    item._cancel_open_orders.assert_not_called()
    item._create_task.assert_not_called()


def test_existing_liquidation_state_does_not_repeat_cancel_or_task_creation():
    item = service()
    item._evaluate_and_commit = Mock(
        return_value=("LIQUIDATING", False, "RISK_LIMIT_STILL_EXCEEDED")
    )
    item._cancel_open_orders = Mock()
    item._create_task = Mock()

    result = item.process_account("A001")

    assert result.retain_dirty is True
    item._cancel_open_orders.assert_not_called()
    item._create_task.assert_not_called()
