from datetime import timedelta
import json
from unittest.mock import Mock

import pytest
from redis.exceptions import ConnectionError

from app.common.exceptions import AuthenticationError, ServiceUnavailableError
from app.common.time_utils import utc_now
from app.realtime.websocket_ticket_service import WebSocketTicketService


def test_ticket_only_stores_hash_key_and_can_be_consumed_once():
    redis_client = Mock()
    redis_client.set.return_value = True
    service = WebSocketTicketService(redis_client, expire_seconds=30)
    expires = utc_now() + timedelta(minutes=10)

    issued = service.create(
        user_id="U001",
        role="USER",
        token_jti="JTI-001",
        token_expiration=expires,
    )

    ticket = issued.ticket
    assert issued.expires_in == 30
    assert ticket not in redis_client.set.call_args.args[0]
    assert redis_client.set.call_args.kwargs == {"ex": 30, "nx": True}
    stored = json.loads(redis_client.set.call_args.args[1])
    assert stored["user_id"] == "U001"
    assert "access_token" not in stored

    redis_client.eval.return_value = redis_client.set.call_args.args[1]
    claims = service.consume(ticket)
    assert claims.user_id == "U001"
    assert claims.token_jti == "JTI-001"
    assert ticket not in redis_client.eval.call_args.args

    redis_client.eval.return_value = None
    with pytest.raises(AuthenticationError) as exc_info:
        service.consume(ticket)
    assert exc_info.value.error_code == "WS_TICKET_INVALID"


def test_ticket_redis_failure_is_fail_closed():
    redis_client = Mock()
    redis_client.set.side_effect = ConnectionError("redis unavailable")
    service = WebSocketTicketService(redis_client)

    with pytest.raises(ServiceUnavailableError) as exc_info:
        service.create(
            user_id="U001",
            role="USER",
            token_jti="JTI",
            token_expiration=utc_now() + timedelta(minutes=1),
        )
    assert exc_info.value.error_code == "WS_TICKET_STORE_UNAVAILABLE"


def test_forged_or_empty_ticket_is_rejected_without_anonymous_fallback():
    redis_client = Mock()
    service = WebSocketTicketService(redis_client)

    with pytest.raises(AuthenticationError):
        service.consume("  ")
    redis_client.eval.assert_not_called()

    redis_client.eval.return_value = None
    with pytest.raises(AuthenticationError):
        service.consume("forged-ticket")
