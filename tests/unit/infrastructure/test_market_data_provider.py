from unittest.mock import Mock

from app.infrastructure.market_data.provider import MarketDataProvider
from app.infrastructure.market_data.remote_feed_client import RemoteFeedClient


def test_remote_feed_client_implements_market_data_provider_boundary():
    provider = RemoteFeedClient(Mock())

    assert isinstance(provider, MarketDataProvider)
