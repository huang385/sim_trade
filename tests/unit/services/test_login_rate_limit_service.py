from unittest.mock import Mock

import pytest
from redis.exceptions import ConnectionError

from app.common.exceptions import RateLimitError, ServiceUnavailableError
from app.services.login_rate_limit_service import LoginRateLimitService


def test_login_ip_rate_limit_allows_at_limit_and_rejects_after():
    redis_client = Mock()
    redis_client.eval.side_effect = [3, 4]
    service = LoginRateLimitService(redis_client, limit=3)

    service.check("127.0.0.1")
    with pytest.raises(RateLimitError) as exc_info:
        service.check("127.0.0.1")

    assert exc_info.value.error_code == "LOGIN_RATE_LIMITED"
    assert redis_client.eval.call_count == 2


def test_login_rate_limit_redis_failure_is_fail_closed():
    redis_client = Mock()
    redis_client.eval.side_effect = ConnectionError("redis unavailable")

    with pytest.raises(ServiceUnavailableError) as exc_info:
        LoginRateLimitService(redis_client).check("127.0.0.1")

    assert exc_info.value.error_code == "LOGIN_PROTECTION_UNAVAILABLE"
