from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.main import app
from app.schemas.market_subscription_schema import (
    MarketPreparationStatus,
    OptionMarketPrepareResponse,
)
from app.services.option_market_pre_subscription_service import (
    get_option_market_pre_subscription_service,
)
from tests.api_auth_helpers import install_admin_auth_overrides


@pytest.fixture
def subscription_api():
    service = Mock()
    database_session = Mock()

    def override_db():
        yield database_session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[
        get_option_market_pre_subscription_service
    ] = lambda: service
    _, authorization = install_admin_auth_overrides()
    try:
        yield TestClient(app), service, authorization, database_session
    finally:
        app.dependency_overrides.clear()


def make_response(status=MarketPreparationStatus.WAITING_MARKET_DATA):
    return OptionMarketPrepareResponse(
        account_id="A001",
        exchange_id="DCE",
        symbol="JD2609-C-4000",
        status=status,
        requested_codes=["JD2609", "JD2609-C-4000"],
        ready_codes=[],
        expires_at=datetime(2026, 8, 4, 1, 3, tzinfo=timezone.utc),
        latest_prices_available=False,
    )


def test_prepare_authorizes_account_and_calls_service(subscription_api):
    client, service, authorization, database_session = subscription_api
    account = SimpleNamespace(account_id="A001")
    authorization.require_account_access.return_value = account
    service.prepare.return_value = make_response()

    response = client.post(
        "/api/market-data/subscriptions/prepare",
        json={
            "account_id": "A001",
            "exchange_id": "dce",
            "symbol": "jd2609-c-4000",
            "direction": "SELL",
            "offset_flag": "OPEN",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "WAITING_MARKET_DATA"
    authorization.require_account_access.assert_called_once()
    call = service.prepare.call_args.kwargs
    assert call["account"] is account
    assert call["request"].symbol == "JD2609-C-4000"
    assert service.prepare.call_args.args == (database_session,)


def test_status_authorizes_account_before_query(subscription_api):
    client, service, authorization, database_session = subscription_api
    service.get_status.return_value = make_response(
        MarketPreparationStatus.READY
    )

    response = client.get(
        "/api/market-data/subscriptions/status",
        params={
            "account_id": "A001",
            "exchange_id": "DCE",
            "symbol": "JD2609-C-4000",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "READY"
    authorization.require_account_access.assert_called_once()
    service.get_status.assert_called_once_with(
        database_session,
        account_id="A001",
        exchange_id="DCE",
        symbol="JD2609-C-4000",
    )
