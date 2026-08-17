from datetime import date
from decimal import Decimal
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.main import app
from app.schemas.instrument_schema import InstrumentCatalogItem
from app.services.instrument_service import get_instrument_service
from tests.api_auth_helpers import install_admin_auth_overrides


@pytest.fixture
def instrument_search_api():
    service = Mock()
    database_session = Mock()

    def override_db():
        yield database_session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_instrument_service] = lambda: service
    install_admin_auth_overrides()
    try:
        yield TestClient(app), service, database_session
    finally:
        app.dependency_overrides.clear()


def test_search_returns_option_fields_and_calls_service(instrument_search_api):
    client, service, database_session = instrument_search_api
    service.search_tradeable_derivatives.return_value = [
        InstrumentCatalogItem(
            order_book_id="CU2609C80000",
            symbol="cu2609C80000",
            exchange_id="SHFE",
            instrument_name="铜2609购80000",
            product_id="CU",
            instrument_type="FUTURES_OPTION",
            underlying_order_book_id="CU2609",
            option_type="CALL",
            strike_price=Decimal("80000"),
            expire_date=date(2026, 8, 25),
            contract_multiplier=Decimal("5"),
            price_tick=Decimal("1"),
        )
    ]

    response = client.get(
        "/api/instruments/search",
        params={"q": " cu ", "limit": 20},
    )

    assert response.status_code == 200
    assert response.json()[0]["instrument_type"] == "FUTURES_OPTION"
    assert response.json()[0]["underlying_order_book_id"] == "CU2609"
    service.search_tradeable_derivatives.assert_called_once_with(
        database_session,
        query=" cu ",
        limit=20,
    )


def test_search_rejects_limit_above_maximum(instrument_search_api):
    client, service, _ = instrument_search_api

    response = client.get(
        "/api/instruments/search",
        params={"q": "CU", "limit": 101},
    )

    assert response.status_code == 422
    service.search_tradeable_derivatives.assert_not_called()
