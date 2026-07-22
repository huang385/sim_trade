from unittest.mock import Mock

from app.services.market_subscription_service import MarketSubscriptionService


def make_index(details):
    index = Mock()
    index.list_all_order_ids.return_value = set(details)
    index.get_active_order.side_effect = details.get
    return index


def test_no_active_orders_produces_empty_desired_set():
    service = MarketSubscriptionService(
        active_order_index=make_index({}),
        debounce_seconds=3,
    )

    assert service.get_desired_codes() == frozenset()


def test_repeated_order_book_id_is_subscribed_only_once():
    service = MarketSubscriptionService(
        active_order_index=make_index(
            {
                "O1": {"order_book_id": "AG2609"},
                "O2": {"order_book_id": "ag2609"},
                "O3": {"order_book_id": "AU2608"},
            }
        ),
        debounce_seconds=3,
    )

    assert service.get_desired_codes() == frozenset({"AG2609", "AU2608"})


def test_add_and_remove_changes_wait_for_debounce():
    service = MarketSubscriptionService(
        active_order_index=make_index({}),
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
