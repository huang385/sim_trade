from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置，支持从项目根目录的 .env 文件和环境变量读取。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

    app_name: str = "sim_trade"
    app_env: str = "dev"
    app_version: str = "0.1.0"
    debug: bool = True

    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432
    postgres_db: str = "sim_trade"
    postgres_user: str = "postgres"
    postgres_password: str = "root"

    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_db: int = 0
    # 必须大于 Stream 的阻塞读取时间，避免正常空闲被误判成读取超时。
    redis_socket_timeout_seconds: float = 10.0

    # 订单事件消费配置。Consumer 名称为空时由主机名和进程号自动生成。
    order_stream_name: str = "stream:orders"
    order_consumer_group: str = "group:order-engine"
    order_consumer_name: str | None = None
    order_consumer_batch_size: int = 100
    order_consumer_block_ms: int = 5000
    order_pending_idle_ms: int = 60000
    order_event_max_retries: int = 10
    order_event_processed_ttl_seconds: int = 604800
    order_event_failure_ttl_seconds: int = 604800
    order_dead_letter_stream: str = "stream:orders:dead-letter"
    order_consumer_retry_interval_seconds: float = 1.0

    # PostgreSQL活动订单游标分页重建批次大小
    active_order_rebuild_batch_size: int = 500

    # 优美利FeedHub行情服务。真实地址和凭证只允许通过.env或环境变量提供。
    remote_market_data_base_url: str = ""
    remote_market_data_api_user: str = ""
    remote_market_data_api_token: str = ""
    remote_market_data_timeout_seconds: float = 3.0
    remote_market_data_verify_ssl: bool = True

    # 行情订阅、重连、本地队列和Redis派生数据配置。
    remote_market_data_subscription_refresh_seconds: float = 1.0
    remote_market_data_subscription_debounce_seconds: float = 3.0
    remote_market_data_reconnect_initial_seconds: float = 1.0
    remote_market_data_reconnect_max_seconds: float = 30.0
    remote_market_data_queue_size: int = 10_000
    remote_market_data_shutdown_drain_timeout_seconds: float = 10.0
    market_tick_stream_name: str = "stream:market-ticks"

    # 行情撮合 Consumer Group。首次创建使用 $，只消费建组后的实时 Tick。
    market_matching_consumer_group: str = "group:matching-engine"
    market_matching_consumer_name: str | None = None
    market_matching_batch_size: int = 100
    market_matching_block_ms: int = 5000
    market_matching_pending_idle_ms: int = 60000
    market_matching_max_retries: int = 10
    market_matching_failure_ttl_seconds: int = 604800
    market_matching_dead_letter_stream: str = "stream:market-ticks:dead-letter"
    market_matching_retry_interval_seconds: float = 1.0
    # 撮合算法由注册器按名称创建；未知名称会让 Worker 在启动阶段失败。
    matching_engine_name: str = "VN"

    # 盘中实时盈亏使用独立行情Consumer Group，不能与撮合组共享消息。
    pnl_consumer_group: str = "group:pnl-engine"
    pnl_consumer_name: str | None = None
    pnl_consumer_batch_size: int = 100
    pnl_consumer_block_ms: int = 5000
    pnl_pending_idle_ms: int = 60000
    pnl_event_max_retries: int = 10
    pnl_failure_ttl_seconds: int = 604800
    pnl_dead_letter_stream: str = "stream:market-ticks:pnl:dead-letter"
    pnl_consumer_retry_interval_seconds: float = 1.0
    # 行情持续消费，但实时盈亏只按该周期合并同一合约的最新行情。
    pnl_calculation_interval_ms: int = 500
    # 订单/撤单/成交Outbox会主动递增缓存版本，60秒TTL只作为最终对账兜底。
    active_position_cache_refresh_ms: int = 60000
    pnl_full_reconciliation_interval_seconds: int = 60
    pnl_worker_lease_ttl_seconds: int = 15
    pnl_worker_lease_renew_seconds: int = 5

    # Redis实时结果按Dirty集合定时批量落库，而不是逐Tick更新PostgreSQL。
    pnl_persist_interval_ms: int = 1000
    pnl_persist_batch_size: int = 500
    pnl_persist_max_batches_per_cycle: int = 10
    pnl_persist_time_budget_ms: int = 800

    # 订单事实事件独立消费组用于提交后立即失效账户和活动持仓缓存。
    pnl_trade_consumer_group: str = "group:pnl-trade-engine"
    pnl_trade_consumer_name: str | None = None
    pnl_trade_consumer_batch_size: int = 100
    pnl_trade_consumer_block_ms: int = 5000
    pnl_trade_pending_idle_ms: int = 60000
    pnl_trade_event_max_retries: int = 10
    pnl_trade_failure_ttl_seconds: int = 604800
    pnl_trade_dead_letter_stream: str = "stream:orders:pnl:dead-letter"
    pnl_trade_retry_interval_seconds: float = 1.0

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.postgres_user}:"
            f"{self.postgres_password}@{self.postgres_host}:"
            f"{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"

settings = Settings()
