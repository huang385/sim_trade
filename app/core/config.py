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
