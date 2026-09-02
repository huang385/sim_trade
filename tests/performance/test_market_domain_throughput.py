"""不依赖外部行情源的高消息量回归，验证拆分后没有串域或消息丢失。"""

from types import SimpleNamespace

import pytest

from app.enums.market_feed_enums import MarketFeedDomain
from app.services.market_subscription_service import MarketSubscriptionService
from app.workers.matching_worker import MatchingWorker


pytestmark = pytest.mark.performance


class BulkConsumer:
    consumer_name = "pressure-consumer"

    def __init__(self, messages):
        self.messages = messages
        self.acked = 0

    def claim_stale_messages(self, **_kwargs):
        return []

    def read_new_messages(self, **_kwargs):
        messages, self.messages = self.messages, []
        return messages

    def acknowledge(self, _message_id):
        self.acked += 1

    def clear_failure(self, _message_id):
        return None


class NoMatchService:
    def __init__(self):
        self.processed = 0

    def process(self, **_kwargs):
        self.processed += 1
        return SimpleNamespace(
            candidate_count=0,
            matched_count=0,
            settled_count=0,
            idempotent_count=0,
        )


def test_matching_worker_acknowledges_100000_ticks_without_loss():
    count = 100_000
    messages = [(f"{index}-0", {}) for index in range(count)]
    consumer = BulkConsumer(messages)
    service = NoMatchService()
    worker = MatchingWorker(
        stream_consumer=consumer,
        matching_service=service,
        batch_size=count,
        block_ms=0,
        pending_idle_ms=60_000,
        max_retries=10,
        retry_interval_seconds=0,
    )

    result = worker.run_once()

    assert result.received == result.acknowledged == count
    assert result.retried == result.dead_lettered == 0
    assert consumer.acked == service.processed == count


class DomainIndex:
    def __init__(self, expected_domain, codes):
        self.expected_domain = expected_domain
        self.codes = codes

    def list_active_contract_codes(self, domain):
        assert domain == self.expected_domain
        return self.codes

    def list_margin_dependency_codes(self):
        return set()


class EmptyPositions:
    def list_active_contract_codes(self):
        return set()

    def list_margin_dependency_codes(self):
        return set()


@pytest.mark.parametrize("domain", list(MarketFeedDomain))
def test_each_domain_deduplicates_50000_subscription_demands(domain):
    unique_codes = {f"CODE{index}" for index in range(25_000)}
    inputs = unique_codes | {code.lower() for code in unique_codes}
    service = MarketSubscriptionService(
        market_domain=domain,
        active_order_index=DomainIndex(domain, inputs),
        active_position_contract_source=EmptyPositions(),
        debounce_seconds=3,
    )

    assert service.get_desired_codes() == frozenset(unique_codes)
