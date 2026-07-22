from unittest.mock import Mock

import pytest

from app.infrastructure.market_data.market_tick_store import MarketTickStoreResult
from app.services.market_data_service import (
    MarketDataService,
    MarketInstrumentSnapshot,
)
from app.services.market_tick_validation_service import MarketTickValidationError
from tests.unit.services.test_market_tick_normalizer import (
    make_data,
    make_instrument,
    make_raw,
    normalize,
)


def make_service(repository=None, store=None):
    repository = repository or Mock()
    store = store or Mock()
    normalizer = Mock()
    validation = Mock()
    return (
        MarketDataService(
            instrument_repository=repository,
            normalizer=normalizer,
            validation_service=validation,
            tick_store=store,
        ),
        repository,
        normalizer,
        validation,
        store,
    )


def test_missing_instrument_does_not_publish():
    service, repository, _normalizer, validation, store = make_service()
    repository.get_by_order_book_id.return_value = None

    with pytest.raises(MarketTickValidationError, match="合约不存在"):
        service.process(Mock(), data=make_data(), raw=make_raw())

    validation.validate_envelope.assert_called_once()
    store.publish.assert_not_called()


def test_valid_tick_is_validated_and_published():
    service, repository, normalizer, validation, store = make_service()
    instrument = make_instrument()
    tick = normalize()
    repository.get_by_order_book_id.return_value = instrument
    normalizer.normalize.return_value = tick
    store.publish.return_value = MarketTickStoreResult.PUBLISHED

    result = service.process(Mock(), data=make_data(), raw=make_raw())

    assert result.action == MarketTickStoreResult.PUBLISHED
    validation.validate.assert_called_once_with(
        tick=tick,
        instrument=MarketInstrumentSnapshot(
            order_book_id="AG2609",
            exchange_id="SHFE",
            symbol="AG2609",
            is_active=True,
        ),
    )
    store.publish.assert_called_once_with(tick)


def test_repeated_ticks_for_same_contract_query_repository_only_once():
    service, repository, normalizer, _validation, store = make_service()
    repository.get_by_order_book_id.return_value = make_instrument()
    normalizer.normalize.return_value = normalize()
    store.publish.return_value = MarketTickStoreResult.PUBLISHED
    db = Mock()

    service.process(db, data=make_data(), raw=make_raw())
    service.process(db, data=make_data(sequence_id=834), raw=make_raw())

    repository.get_by_order_book_id.assert_called_once_with(db, "AG2609")
    assert normalizer.normalize.call_count == 2


def test_warm_cache_processes_tick_without_creating_database_session():
    service, repository, normalizer, _validation, store = make_service()
    repository.get_by_order_book_id.return_value = make_instrument()
    normalizer.normalize.return_value = normalize()
    store.publish.return_value = MarketTickStoreResult.PUBLISHED
    service.process(Mock(), data=make_data(), raw=make_raw())
    session_factory = Mock(side_effect=AssertionError("不应创建Session"))

    service.process_with_session_factory(
        session_factory,
        data=make_data(sequence_id=834),
        raw=make_raw(),
    )

    session_factory.assert_not_called()


def test_subscription_refresh_bulk_warms_existing_and_missing_contracts():
    service, repository, normalizer, _validation, store = make_service()
    repository.list_by_order_book_ids.return_value = [make_instrument()]
    normalizer.normalize.return_value = normalize()
    store.publish.return_value = MarketTickStoreResult.PUBLISHED
    db = Mock()

    service.refresh_instrument_cache(db, {"AG2609", "MISSING"})
    service.process_with_session_factory(
        Mock(side_effect=AssertionError("预热后不应创建Session")),
        data=make_data(),
        raw=make_raw(),
    )
    with pytest.raises(MarketTickValidationError, match="合约不存在"):
        service.process_with_session_factory(
            Mock(side_effect=AssertionError("负缓存不应创建Session")),
            data=make_data(code="MISSING"),
            raw=make_raw(),
        )

    repository.list_by_order_book_ids.assert_called_once_with(
        db,
        {"AG2609", "MISSING"},
    )


def test_cache_does_not_expire_or_repeatedly_query_database():
    service, repository, normalizer, _validation, store = make_service()
    repository.get_by_order_book_id.return_value = make_instrument()
    normalizer.normalize.return_value = normalize()
    store.publish.return_value = MarketTickStoreResult.PUBLISHED
    db = Mock()

    for sequence_id in range(1_000):
        service.process(
            db,
            data=make_data(sequence_id=sequence_id),
            raw=make_raw(),
        )

    repository.get_by_order_book_id.assert_called_once_with(db, "AG2609")


def test_missing_contract_is_negative_cached():
    service, repository, *_ = make_service()
    repository.get_by_order_book_id.return_value = None
    db = Mock()

    for _ in range(2):
        with pytest.raises(MarketTickValidationError, match="合约不存在"):
            service.process(db, data=make_data(), raw=make_raw())

    repository.get_by_order_book_id.assert_called_once_with(db, "AG2609")
