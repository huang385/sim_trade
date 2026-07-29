from unittest.mock import Mock

from app.services.market_subscription_service import MarketSubscriptionService


def make_index(details):
    index = Mock()
    index.list_all_order_ids.return_value = set(details)
    index.get_active_order.side_effect = details.get
    index.list_active_contract_codes.return_value = {
        detail["order_book_id"]
        for detail in details.values()
        if detail.get("order_book_id")
    }
    return index


def make_position_source(codes=()):
    source = Mock()
    source.list_active_contract_codes.return_value = set(codes)
    return source


def test_no_active_orders_produces_empty_desired_set():
    service = MarketSubscriptionService(
        active_order_index=make_index({}),
        active_position_contract_source=make_position_source(),
        debounce_seconds=3,
    )

    assert service.get_desired_codes() == frozenset()


def test_repeated_order_book_id_is_subscribed_only_once():
    index = make_index(
            {
                "O1": {"order_book_id": "AG2609"},
                "O2": {"order_book_id": "ag2609"},
                "O3": {"order_book_id": "AU2608"},
            }
        )
    service = MarketSubscriptionService(
        active_order_index=index,
        active_position_contract_source=make_position_source(),
        debounce_seconds=3,
    )

    assert service.get_desired_codes() == frozenset({"AG2609", "AU2608"})
    index.list_active_contract_codes.assert_called_once()
    index.get_active_order.assert_not_called()


def test_active_positions_are_subscribed_without_active_orders():
    service = MarketSubscriptionService(
        active_order_index=make_index({}),
        active_position_contract_source=make_position_source(
            {"jd2609", "AG2612"}
        ),
        debounce_seconds=3,
    )

    assert service.get_desired_codes() == frozenset(
        {"JD2609", "AG2612"}
    )


def test_active_orders_and_positions_are_merged_and_deduplicated():
    service = MarketSubscriptionService(
        active_order_index=make_index(
            {
                "O1": {"order_book_id": "JD2609"},
                "O2": {"order_book_id": "A2609"},
            }
        ),
        active_position_contract_source=make_position_source(
            {"jd2609", "AG2612"}
        ),
        debounce_seconds=3,
    )

    assert service.get_desired_codes() == frozenset(
        {"JD2609", "A2609", "AG2612"}
    )


def test_add_and_remove_changes_wait_for_debounce():
    service = MarketSubscriptionService(
        active_order_index=make_index({}),
        active_position_contract_source=make_position_source(),
        debounce_seconds=3,
    )
    desired = frozenset({"AG2609"})

    assert service.observe(desired, now=1) is None
    assert service.observe(desired, now=3.9) is None
    change = service.observe(desired, now=4)
    assert change.codes == desired
    service.mark_applied(desired)

    assert service.observe(frozenset(), now=5) is None
    change = service.observe(frozenset(), now=8)
    assert change.codes == frozenset()


def test_partial_subscription_receipt_tracks_success_and_failure_reasons():
    service = MarketSubscriptionService(
        active_order_index=make_index({}),
        active_position_contract_source=make_position_source(),
        debounce_seconds=3,
    )
    generation = service.mark_requested(
        frozenset({"OK", "MISSING", "NOTLIVE", "FAILED"})
    )

    state = service.apply_subscription_report(
        {
            "contracts": {
                "OK": {"exists": True, "is_live": True, "subscribed": True},
                "MISSING": {"exists": False, "is_live": False, "subscribed": False},
                "NOTLIVE": {"exists": True, "is_live": False, "subscribed": False},
                "FAILED": {"exists": True, "is_live": True, "subscribed": False},
            }
        },
        generation=generation,
    )

    assert state.subscribed_codes == frozenset({"OK"})
    assert state.failure_reasons == {
        "MISSING": "CONTRACT_NOT_FOUND",
        "NOTLIVE": "CONTRACT_NOT_LIVE",
        "FAILED": "SUBSCRIBE_FAILED",
    }


def test_duplicate_and_out_of_order_receipts_keep_success_monotonic():
    service = MarketSubscriptionService(
        active_order_index=make_index({}),
        active_position_contract_source=make_position_source(),
        debounce_seconds=3,
    )
    generation = service.mark_requested(frozenset({"AG2609"}))
    failure = {
        "contracts": {
            "AG2609": {"exists": True, "is_live": True, "subscribed": False}
        }
    }
    success = {
        "contracts": {
            "AG2609": {"exists": True, "is_live": True, "subscribed": True}
        }
    }

    service.apply_subscription_report(failure, generation=generation)
    service.apply_subscription_report(success, generation=generation)
    state = service.apply_subscription_report(failure, generation=generation)

    assert state.all_subscribed is True
    assert state.failed_codes == frozenset()


def test_old_generation_receipt_does_not_change_new_request():
    service = MarketSubscriptionService(
        active_order_index=make_index({}),
        active_position_contract_source=make_position_source(),
        debounce_seconds=3,
    )
    old_generation = service.mark_requested(frozenset({"OLD"}))
    service.mark_requested(frozenset({"NEW"}))

    state = service.apply_subscription_report(
        {
            "contracts": {
                "OLD": {"exists": True, "is_live": True, "subscribed": True}
            }
        },
        generation=old_generation,
    )

    assert state.requested_codes == frozenset({"NEW"})
    assert state.subscribed_codes == frozenset()
