import logging

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.logging_config import setup_logging
from app.core.redis_client import redis_client
from app.infrastructure.active_order_index import ActiveOrderIndex
from app.repositories.order_repository import OrderRepository
from app.services.active_order_rebuild_service import (
    ActiveOrderRebuildService,
)


logger = logging.getLogger(__name__)


def main() -> None:
    """命令行入口：从PostgreSQL一次性重建Redis活动订单索引。"""

    setup_logging()
    service = ActiveOrderRebuildService(
        order_repository=OrderRepository(),
        active_order_index=ActiveOrderIndex(redis_client),
        batch_size=settings.active_order_rebuild_batch_size,
    )
    try:
        with SessionLocal() as db:
            result = service.rebuild(db)
        logger.info(
            "活动订单索引重建完成 scanned=%s upserted=%s removed=%s "
            "skipped=%s failed=%s",
            result.scanned,
            result.upserted,
            result.removed,
            result.skipped,
            result.failed,
        )
    finally:
        redis_client.close()


if __name__ == "__main__":
    main()
