from decimal import Decimal

from pydantic_settings import BaseSettings, SettingsConfigDict


# 这些字符串已经公开出现在示例配置或常见部署模板中，不能作为签名密钥。
# 判断结果只用于拒绝配置，任何异常和日志都不得回显实际Secret。
KNOWN_UNSAFE_AUTH_JWT_SECRETS = frozenset(
    {
        "replace-with-at-least-32-random-bytes",
        "change-me-to-at-least-32-random-bytes",
        "your-secret-key-change-me",
        "please-change-this-secret",
    }
)


def is_unsafe_auth_jwt_secret(secret: str) -> bool:
    """
    判断JWT Secret是否属于明确不可接受的配置。

    这里只做最低安全门槛：空值、UTF-8长度不足、项目公开占位值，以及由
    极少字符机械重复构成的明显示例值。不会尝试自行实现密码学熵估算。
    """

    normalized = secret.strip()
    if len(normalized.encode("utf-8")) < 32:
        return True
    if normalized.lower() in KNOWN_UNSAFE_AUTH_JWT_SECRETS:
        return True
    return len(set(normalized)) <= 4


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

    # 认证密钥必须由部署环境提供。空字符串只允许应用启动和执行公开健康检查；
    # 任何签发或校验Token的操作都会明确拒绝未配置或强度不足的密钥。
    auth_jwt_secret: str = ""
    auth_jwt_algorithm: str = "HS256"
    auth_access_token_expire_minutes: int = 15
    auth_refresh_token_expire_days: int = 7
    auth_issuer: str = "sim-trade"
    auth_audience: str = "sim-trade-client"
    auth_max_login_failures: int = 5
    auth_login_lock_minutes: int = 15
    auth_login_rate_limit_per_minute: int = 30
    auth_refresh_cookie_name: str = "sim_trade_refresh"
    auth_refresh_cookie_secure: bool = False
    auth_refresh_cookie_samesite: str = "lax"

    # 期权能力全部采用失败关闭的双层开关。系统产品开关开启后，账户自身
    # option_trading_enabled仍必须为True。股指期权卖方和行权本阶段固定
    # 保持关闭，代码只预留模型与纯计算能力。
    option_trading_enabled: bool = False
    commodity_option_trading_enabled: bool = False
    index_option_buy_trading_enabled: bool = False
    index_option_short_trading_enabled: bool = False
    option_exercise_enabled: bool = False
    option_collateral_ratio: Decimal = Decimal("0")

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

    # 期权加入后沿用同一个单写者估值链路；独立命名用于逐步替换旧PNL
    # 配置，默认值与现有持久化周期一致。
    valuation_persist_interval_ms: int = 1000
    valuation_persist_batch_size: int = 500

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

    # WebSocket实时推送使用独立事件流。订单Outbox由投影Worker转换后写入，
    # PnL快照则与该事件流在同一个Redis脚本中原子更新。
    realtime_event_stream_name: str = "stream:realtime-events"
    realtime_event_stream_maxlen: int = 1_000_000
    realtime_projection_consumer_group: str = "group:realtime-projection"
    realtime_projection_consumer_name: str | None = None
    realtime_projection_batch_size: int = 100
    realtime_projection_block_ms: int = 5000
    realtime_projection_pending_idle_ms: int = 60000
    realtime_projection_max_retries: int = 10
    realtime_projection_failure_ttl_seconds: int = 604800
    realtime_projection_dead_letter_stream: str = (
        "stream:orders:realtime:dead-letter"
    )

    # 第一版Gateway固定单实例。独立Consumer Group只服务这一个活动实例，
    # 租约用于阻止误启动第二个实例造成连接与事件分片不一致。
    ws_gateway_consumer_group: str = "group:ws-gateway"
    ws_gateway_consumer_name: str | None = None
    ws_gateway_batch_size: int = 100
    ws_gateway_block_ms: int = 1000
    ws_gateway_pending_idle_ms: int = 60000
    ws_gateway_dead_letter_stream: str = (
        "stream:realtime-events:dead-letter"
    )
    ws_ticket_expire_seconds: int = 30
    ws_heartbeat_interval_seconds: int = 20
    ws_heartbeat_timeout_seconds: int = 60
    ws_auth_recheck_interval_seconds: int = 60
    ws_send_queue_size: int = 500
    ws_snapshot_buffer_size: int = 1000
    ws_max_subscriptions_per_connection: int = 20
    ws_max_connections_per_user: int = 5
    ws_gateway_lease_ttl_seconds: int = 30
    ws_gateway_lease_renew_seconds: int = 10
    ws_gateway_host: str = "0.0.0.0"
    ws_gateway_port: int = 8001

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

    def validate_runtime_security(self) -> None:
        """
        校验部署环境的认证安全底线。

        开发环境允许暂不配置JWT，以便公开健康检查仍可启动；生产环境必须
        在应用启动前同时提供合格Secret和Secure Refresh Cookie。
        """

        if self.app_env.strip().lower() not in {"prod", "production"}:
            return
        if is_unsafe_auth_jwt_secret(self.auth_jwt_secret):
            raise ValueError("生产环境JWT认证密钥未配置或强度不足")
        if not self.auth_refresh_cookie_secure:
            raise ValueError("生产环境Refresh Cookie必须启用Secure")
        if self.debug:
            raise ValueError("生产环境必须关闭Debug模式")

settings = Settings()
