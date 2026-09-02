"""现金证券撮合进程入口。"""

from app.enums.market_feed_enums import MarketFeedDomain
from app.workers.matching_worker import run_matching_worker


def main() -> None:
    run_matching_worker(MarketFeedDomain.SECURITIES_MARKET)


if __name__ == "__main__":
    main()
