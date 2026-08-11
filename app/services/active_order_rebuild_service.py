import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.enums.order_enums import LIMIT_LIKE_ORDER_TYPES, OffsetFlag, OrderStatus
from app.infrastructure.active_order_index import ActiveOrderIndex
from app.repositories.order_repository import OrderRepository


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActiveOrderRebuildResult:
    """一次活动订单索引重建的统计结果。"""

    scanned: int = 0
    upserted: int = 0
    removed: int = 0
    skipped: int = 0
    failed: int = 0


class ActiveOrderRebuildService:
    """
    以PostgreSQL orders为事实来源，对账并修复Redis活动订单派生索引。

    重建只读取数据库，不修改订单、账户、Outbox、成交或持仓。数据库使用
    id游标分页；Redis通过active_orders:all对账，禁止KEYS扫描详情键。
    """

    ACTIVE_STATUSES = {
        OrderStatus.ACCEPTED.value,
        OrderStatus.PARTIALLY_FILLED.value,
    }
    SUPPORTED_OFFSET_FLAGS = {
        OffsetFlag.OPEN.value,
        OffsetFlag.CLOSE.value,
        OffsetFlag.CLOSE_TODAY.value,
        OffsetFlag.CLOSE_YESTERDAY.value,
    }

    def __init__(
        self,
        *,
        order_repository: OrderRepository,
        active_order_index: ActiveOrderIndex,
        batch_size: int = 500,
    ):
        self.order_repository = order_repository
        self.active_order_index = active_order_index
        self.batch_size = batch_size

    @classmethod
    def is_active_order(cls, order) -> bool:
        """按数据库最新状态判断订单是否仍应等待撮合。"""

        return bool(
            order is not None
            and order.status in cls.ACTIVE_STATUSES
            and order.remaining_volume > 0
            and order.order_type in LIMIT_LIKE_ORDER_TYPES
            and order.offset_flag in cls.SUPPORTED_OFFSET_FLAGS
        )

    def rebuild(self, db: Session) -> ActiveOrderRebuildResult:
        """分页恢复有效订单，并二次确认后清理Redis多余索引。"""

        redis_snapshot = set(self.active_order_index.list_all_order_ids())
        database_active_ids: set[str] = set()
        scanned = upserted = removed = skipped = failed = 0
        last_id = 0

        while True:
            orders = self.order_repository.list_active_after_id(
                db,
                last_id=last_id,
                batch_size=self.batch_size,
            )
            if not orders:
                break
            for order in orders:
                scanned += 1
                last_id = order.id
                database_active_ids.add(order.order_id)
                try:
                    self.active_order_index.upsert_active_order_for_rebuild(
                        order
                    )
                    upserted += 1
                except Exception:
                    failed += 1
                    logger.exception(
                        "活动订单索引写入失败 order_id=%s",
                        order.order_id,
                    )

        # 只清理由重建开始时Redis快照中存在、但分页结果中不存在的编号。
        # 清理前再次读取PostgreSQL，避免并发Consumer新注册的合法订单被误删。
        for order_id in redis_snapshot - database_active_ids:
            current_order = self.order_repository.get_by_order_id(db, order_id)
            if self.is_active_order(current_order):
                database_active_ids.add(order_id)
                try:
                    self.active_order_index.upsert_active_order_for_rebuild(
                        current_order
                    )
                    upserted += 1
                    skipped += 1
                except Exception:
                    failed += 1
                    logger.exception(
                        "并发新增活动订单索引修复失败 order_id=%s",
                        order_id,
                    )
                continue

            detail = self.active_order_index.get_active_order(order_id)
            try:
                if all(
                    detail.get(field)
                    for field in (
                        "account_id",
                        "exchange_id",
                        "symbol",
                    )
                ):
                    self.active_order_index.remove_active_order(
                        order_id=order_id,
                        account_id=detail["account_id"],
                        exchange_id=detail["exchange_id"],
                        symbol=detail["symbol"],
                    )
                else:
                    self.active_order_index.remove_orphan_order_id(order_id)
                    logger.warning(
                        "活动订单详情缺失，已清理全局孤立成员；"
                        "未知账户和合约Set需后续对账 order_id=%s",
                        order_id,
                    )
                removed += 1
            except Exception:
                failed += 1
                logger.exception(
                    "多余活动订单索引清理失败 order_id=%s",
                    order_id,
                )

        # 新旧事件与重建可能并发运行；这里只通过Lua删除合约订单Set已经为空
        # 的成员，不做DEL后整集合覆盖，避免抹掉扫描期间刚注册的新订单。
        try:
            self.active_order_index.reconcile_active_contracts()
        except Exception:
            failed += 1
            logger.exception("活动订单合约索引对账失败")

        return ActiveOrderRebuildResult(
            scanned=scanned,
            upserted=upserted,
            removed=removed,
            skipped=skipped,
            failed=failed,
        )
