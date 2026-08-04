import asyncio
from unittest.mock import AsyncMock, Mock

from app.realtime.event_consumer import RealtimeEventConsumer
from app.realtime.gateway_lease import GatewayLease
from app.realtime.gateway_runtime import GatewayRuntime


def test_fenced_ack_refuses_old_owner():
    redis_client = Mock()
    redis_client.eval.return_value = [0, 0]
    lease = GatewayLease(redis_client, key="lease", ttl_seconds=10)

    owned, acknowledged = lease.acknowledge_if_owned(
        owner_id="old",
        stream_name="events",
        group_name="gateway",
        message_ids=["1-0"],
    )

    assert owned is False
    assert acknowledged == 0
    script = redis_client.eval.call_args.args[0]
    assert "XACK" in script
    assert "GET" in script


def test_consumer_losing_lease_does_not_route_or_ack():
    stream_consumer = Mock(
        stream_name="events",
        group_name="gateway",
    )
    router = Mock(route=AsyncMock())
    lease = Mock()
    lease.is_owner.return_value = False
    on_lost = AsyncMock()
    consumer = RealtimeEventConsumer(
        consumer=stream_consumer,
        router=router,
        lease=lease,
        owner_id="old",
        on_lease_lost=on_lost,
    )

    asyncio.run(consumer._handle("1-0", {"payload": "{}"}))

    router.route.assert_not_awaited()
    stream_consumer.acknowledge.assert_not_called()
    on_lost.assert_awaited_once()


def test_gateway_consumer_group_starts_at_stream_tail_only_once():
    runtime = GatewayRuntime()

    assert runtime.consumer.consumer.group_start_id == "$"
