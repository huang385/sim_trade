"""期货、商品期权、指数和股指期权行情进程入口。"""

from app.enums.market_feed_enums import MarketFeedDomain
from app.workers.market_data_subscriber_worker import run_worker


def main() -> None:
    run_worker(MarketFeedDomain.FUTURES_MARKET)


if __name__ == "__main__":
    main()
