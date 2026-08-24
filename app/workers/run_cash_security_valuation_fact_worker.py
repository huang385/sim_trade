"""Entry point for the cash-security fact-to-dirty consumer."""

import signal

from app.core.redis_client import redis_client
from app.core.config import settings
from app.infrastructure.order_stream_consumer import OrderStreamConsumer
from app.infrastructure.redis_keys import CASH_VALUATION_FACT_CONSUMER_GROUP
from app.workers.cash_security_valuation_worker import (
    CashSecurityValuationFactWorker,
    _consumer_name,
    build_service,
)


def main() -> None:
    worker = CashSecurityValuationFactWorker(
        stream_consumer=OrderStreamConsumer(
            redis_client,
            stream_name=settings.order_stream_name,
            group_name=CASH_VALUATION_FACT_CONSUMER_GROUP,
            consumer_name=_consumer_name("cash-val-fact"),
            dead_letter_stream="stream:orders:cash-valuation:dead-letter",
            failure_ttl_seconds=settings.pnl_trade_failure_ttl_seconds,
        ),
        service=build_service(),
    )
    signal.signal(signal.SIGINT, worker.request_stop)
    signal.signal(signal.SIGTERM, worker.request_stop)
    try:
        worker.run_forever()
    finally:
        redis_client.close()


if __name__ == "__main__":
    main()
