"""Entry point for the cash-security tick-to-dirty consumer."""

import signal

from app.core.redis_client import redis_client
from app.workers.cash_security_valuation_worker import (
    CashSecurityValuationTickWorker,
    build_service,
)


def main() -> None:
    worker = CashSecurityValuationTickWorker(redis=redis_client, service=build_service())
    signal.signal(signal.SIGINT, worker.request_stop)
    signal.signal(signal.SIGTERM, worker.request_stop)
    try:
        worker.run_forever()
    finally:
        redis_client.close()


if __name__ == "__main__":
    main()
