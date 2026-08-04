from types import SimpleNamespace

import pytest

from app.common.exceptions import AuthorizationError, BusinessValidationError
from app.realtime.subscription_service import (
    RealtimeUserIdentity,
    SubscriptionService,
)


class FakeAccountRepository:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def list_by_account_ids(self, _db, account_ids):
        self.calls += 1
        return [row for row in self.rows if row.account_id in account_ids]


def test_subscription_authorizes_all_accounts_with_one_batch_query():
    repository = FakeAccountRepository(
        [
            SimpleNamespace(account_id="A001", user_id="U001"),
            SimpleNamespace(account_id="A002", user_id="U001"),
        ]
    )
    service = SubscriptionService(repository, max_subscriptions=3)

    result = service.authorize(
        object(),
        identity=RealtimeUserIdentity("U001", "USER"),
        requested_account_ids=["A001", "A002"],
        existing_account_ids=set(),
    )

    assert result == frozenset({"A001", "A002"})
    assert repository.calls == 1


def test_mixed_owned_and_foreign_subscription_is_rejected_atomically():
    repository = FakeAccountRepository(
        [
            SimpleNamespace(account_id="A001", user_id="U001"),
            SimpleNamespace(account_id="B001", user_id="U002"),
        ]
    )
    service = SubscriptionService(repository)

    with pytest.raises(AuthorizationError):
        service.authorize(
            object(),
            identity=RealtimeUserIdentity("U001", "USER"),
            requested_account_ids=["A001", "B001"],
            existing_account_ids=set(),
        )
    assert repository.calls == 1


def test_admin_can_subscribe_existing_accounts_but_not_guess_missing_id():
    repository = FakeAccountRepository(
        [SimpleNamespace(account_id="A001", user_id="U001")]
    )
    service = SubscriptionService(repository)
    identity = RealtimeUserIdentity("ADMIN", "ADMIN")

    assert service.authorize(
        object(),
        identity=identity,
        requested_account_ids=["A001"],
        existing_account_ids=set(),
    ) == frozenset({"A001"})
    with pytest.raises(AuthorizationError):
        service.authorize(
            object(),
            identity=identity,
            requested_account_ids=["MISSING"],
            existing_account_ids=set(),
        )


def test_subscription_limit_includes_existing_accounts():
    service = SubscriptionService(FakeAccountRepository([]), max_subscriptions=2)
    with pytest.raises(BusinessValidationError) as exc_info:
        service.authorize(
            object(),
            identity=RealtimeUserIdentity("U001", "USER"),
            requested_account_ids=["A002", "A003"],
            existing_account_ids={"A001"},
        )
    assert exc_info.value.error_code == "WS_SUBSCRIPTION_LIMIT_EXCEEDED"
