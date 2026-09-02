"""衍生品撮合进程入口。"""

from app.enums.market_feed_enums import MarketFeedDomain
from app.workers.matching_worker import run_matching_worker


def main() -> None:
    run_matching_worker(MarketFeedDomain.FUTURES_MARKET)


if __name__ == "__main__":
    main()
