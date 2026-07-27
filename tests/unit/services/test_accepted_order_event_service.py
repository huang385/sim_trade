import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services.accepted_order_event_service import (
    AcceptedOrderEventService,
    OrderEventValidationError,
    UnsupportedOrderEventError,
)


def make_order(**overrides):
    values = {
        "order_id": "O-1",
        "account_id": "A001",
        "exchange_id": "SHFE",
        "symbol": "RB2610",
        "status": "ACCEPTED",
        "remaining_volume": 2,
        "order_type": "LIMIT",
        "offset_flag": "OPEN",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_fields(**payload_overrides):
    payload = {
        "order_id": "O-1",
        "account_id": "A001",
        "exchange_id": "SHFE",
        "symbol": "RB2610",
    }
    payload.update(payload_overrides)
    return {
        "event_id": "EVT-1",
        "event_type": "ORDER_ACCEPTED",
        "payload": json.dumps(payload),
    }


def make_service(order):
    repository = Mock()
    repository.get_by_order_id.return_value = order
    active_index = Mock()
    active_index.add_active_order.return_value = True
    service = AcceptedOrderEventService(
        order_repository=repository,
        active_order_index=active_index,
        processed_ttl_seconds=604800,
    )
    return service, repository, active_index


def test_accepted_order_is_registered():
    service, _, active_index = make_service(make_order())

    result = service.process(Mock(), make_fields())

    assert result.action == "REGISTERED"
    active_index.add_active_order.assert_called_once()
    active_index.remove_active_order.assert_not_called()


def test_partial_fill_event_updates_active_index_from_database_truth():
    service, _, active_index = make_service(
        make_order(status="PARTIALLY_FILLED", remaining_volume=1)
    )
    fields = make_fields(event_type="ORDER_PARTIALLY_FILLED")
    fields["event_type"] = "ORDER_PARTIALLY_FILLED"

    result = service.process(Mock(), fields)

    assert result.action == "UPDATED"
    active_index.add_active_order.assert_called_once()


def test_filled_event_removes_all_active_indexes():
    service, _, active_index = make_service(
        make_order(status="FILLED", remaining_volume=0)
    )
    fields = make_fields(event_type="ORDER_FILLED")
    fields["event_type"] = "ORDER_FILLED"

    result = service.process(Mock(), fields)

    assert result.action == "REMOVED"
    active_index.remove_active_order.assert_called_once()


def test_trade_created_is_known_passthrough_event():
    service, repository, active_index = make_service(make_order())
    fields = make_fields(event_type="TRADE_CREATED")
    fields["event_type"] = "TRADE_CREATED"

    result = service.process(Mock(), fields)

    assert result.action == "IGNORED_TRADE_EVENT"
    repository.get_by_order_id.assert_not_called()
    active_index.add_active_order.assert_not_called()


def test_partially_filled_order_with_remaining_volume_is_registered():
    service, _, active_index = make_service(
        make_order(status="PARTIALLY_FILLED", remaining_volume=1)
    )

    service.process(Mock(), make_fields())

    active_index.add_active_order.assert_called_once()


def test_duplicate_event_is_successful_and_does_not_duplicate_business_effect():
    service, _, active_index = make_service(make_order())
    active_index.add_active_order.return_value = False

    result = service.process(Mock(), make_fields())

    assert result.action == "DUPLICATE"


def test_missing_database_order_does_not_create_index():
    service, _, active_index = make_service(None)

    result = service.process(Mock(), make_fields())

    assert result.action == "ORDER_NOT_FOUND"
    active_index.add_active_order.assert_not_called()
    active_index.remove_active_order.assert_not_called()


@pytest.mark.parametrize(
    "status",
    ["CANCELLED", "PARTIALLY_CANCELLED", "REJECTED", "FILLED"],
)
def test_terminal_order_removes_old_index(status):
    service, _, active_index = make_service(make_order(status=status))

    result = service.process(Mock(), make_fields())

    assert result.action == "REMOVED"
    active_index.remove_active_order.assert_called_once()
    active_index.add_active_order.assert_not_called()


def test_zero_remaining_volume_removes_old_index():
    service, _, active_index = make_service(make_order(remaining_volume=0))

    service.process(Mock(), make_fields())

    active_index.remove_active_order.assert_called_once()


@pytest.mark.parametrize(
    ("field", "value"),
    [("order_type", "MARKET"), ("offset_flag", "UNKNOWN"), ("status", "NEW")],
)
def test_non_active_order_shape_is_not_registered(field, value):
    service, _, active_index = make_service(make_order(**{field: value}))

    service.process(Mock(), make_fields())

    active_index.remove_active_order.assert_called_once()


def test_event_account_mismatch_is_rejected():
    service, _, active_index = make_service(make_order(account_id="A002"))

    with pytest.raises(OrderEventValidationError, match="账户"):
        service.process(Mock(), make_fields())

    active_index.add_active_order.assert_not_called()


def test_invalid_payload_json_is_rejected():
    service, _, _ = make_service(make_order())
    fields = make_fields()
    fields["payload"] = "not-json"

    with pytest.raises(OrderEventValidationError, match="合法JSON"):
        service.process(Mock(), fields)


def test_missing_order_id_is_rejected():
    service, _, _ = make_service(make_order())

    with pytest.raises(OrderEventValidationError, match="order_id"):
        service.process(Mock(), make_fields(order_id=""))


def test_unknown_event_type_is_classified_for_dead_letter():
    service, _, _ = make_service(make_order())
    fields = make_fields()
    fields["event_type"] = "ORDER_UNKNOWN"

    with pytest.raises(UnsupportedOrderEventError):
        service.process(Mock(), fields)


@pytest.mark.parametrize(
    ("event_type", "status"),
    [
        ("ORDER_CANCELLED", "CANCELLED"),
        ("ORDER_PARTIALLY_CANCELLED", "PARTIALLY_CANCELLED"),
    ],
)
def test_cancel_events_remove_all_active_indexes(event_type, status):
    service, _, active_index = make_service(
        make_order(status=status, remaining_volume=0)
    )
    fields = make_fields()
    fields["event_type"] = event_type

    result = service.process(Mock(), fields)

    assert result.action == "REMOVED"
    active_index.remove_active_order.assert_called_once_with(
        order_id="O-1",
        account_id="A001",
        exchange_id="SHFE",
        symbol="RB2610",
        event_id="EVT-1",
        processed_ttl_seconds=604800,
    )
