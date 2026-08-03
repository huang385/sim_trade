from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services.market_data_code_mapping_service import (
    MarketDataCodeMappingService,
)


def instrument(order_book_id: str, instrument_type: str):
    return SimpleNamespace(
        order_book_id=order_book_id,
        instrument_type=instrument_type,
    )


def mapping(market_data_code: str):
    return SimpleNamespace(market_data_code=market_data_code)


def make_service(rows):
    repository = Mock()
    repository.list_instruments_with_mapping.return_value = rows
    return MarketDataCodeMappingService(repository), repository


def test_commodity_option_without_explicit_mapping_uses_feedhub_format():
    service, _repository = make_service(
        [(instrument("JD2609-C-4000", "FUTURES_OPTION"), None)]
    )

    snapshot = service.build_snapshot(Mock(), {"JD2609-C-4000"})

    assert snapshot.to_source("JD2609-C-4000") == "JD2609C4000"
    assert snapshot.to_internal("JD2609C4000") == "JD2609-C-4000"


def test_index_option_without_explicit_mapping_uses_feedhub_format():
    service, _repository = make_service(
        [(instrument("IO2608-P-4000", "INDEX_OPTION"), None)]
    )

    snapshot = service.build_snapshot(Mock(), {"IO2608-P-4000"})

    assert snapshot.source_codes == frozenset({"IO2608P4000"})


def test_futures_without_mapping_keeps_internal_code():
    service, _repository = make_service(
        [(instrument("JD2609", "FUTURES"), None)]
    )

    snapshot = service.build_snapshot(Mock(), {"JD2609"})

    assert snapshot.to_source("JD2609") == "JD2609"
    assert snapshot.to_internal("JD2609") == "JD2609"


def test_explicit_mapping_overrides_default_option_format():
    service, _repository = make_service(
        [
            (
                instrument("JD2609-C-4000", "FUTURES_OPTION"),
                mapping("VENDOR-JD-C4000"),
            )
        ]
    )

    snapshot = service.build_snapshot(Mock(), {"JD2609-C-4000"})

    assert snapshot.to_source("JD2609-C-4000") == "VENDOR-JD-C4000"
    assert snapshot.to_internal("VENDOR-JD-C4000") == "JD2609-C-4000"


def test_source_code_collision_is_rejected():
    service, _repository = make_service(
        [
            (instrument("OPTION-A", "FUTURES_OPTION"), mapping("SAME")),
            (instrument("OPTION-B", "FUTURES_OPTION"), mapping("SAME")),
        ]
    )

    with pytest.raises(ValueError, match="同一行情源代码"):
        service.build_snapshot(Mock(), {"OPTION-A", "OPTION-B"})


def test_mapping_repository_is_queried_once_for_whole_subscription():
    service, repository = make_service(
        [
            (instrument("JD2609-C-4000", "FUTURES_OPTION"), None),
            (instrument("SI2704-P-8900", "FUTURES_OPTION"), None),
        ]
    )
    db = Mock()

    service.build_snapshot(
        db,
        {"JD2609-C-4000", "SI2704-P-8900"},
    )

    repository.list_instruments_with_mapping.assert_called_once_with(
        db,
        data_source="YML_FEEDHUB",
        order_book_ids={"JD2609-C-4000", "SI2704-P-8900"},
    )
