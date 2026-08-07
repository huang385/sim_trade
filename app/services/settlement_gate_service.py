from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.common.exceptions import BusinessRuleError
from app.repositories.daily_settlement_repository import DailySettlementRepository


# 所有下单、成交和手工日终进程共用同一个 PostgreSQL advisory lock 域。
# 普通交易取得事务级共享锁，日终命令持有会话级排他锁直到整批退出。
DAILY_SETTLEMENT_ADVISORY_LOCK_KEY = 2_026_080_601


class SettlementGateService:
    """数据库结算闸门，消除“检查状态后并发写入”的竞态。"""

    def __init__(
        self, repository: DailySettlementRepository | None = None
    ) -> None:
        self.repository = repository or DailySettlementRepository()

    @staticmethod
    def acquire_shared_transaction_lock(db: Session) -> None:
        if db.get_bind().dialect.name == "postgresql":
            db.execute(
                text("SELECT pg_advisory_xact_lock_shared(:lock_key)"),
                {"lock_key": DAILY_SETTLEMENT_ADVISORY_LOCK_KEY},
            )

    def ensure_trading_open(
        self, db: Session, *, trading_day: date | None = None
    ) -> None:
        """共享锁覆盖当前事务，并按数据库批次事实判断是否允许交易。"""

        dialect_name = getattr(getattr(db.get_bind(), "dialect", None), "name", None)
        # 一些纯单元测试使用不具备真实绑定的 Session 替身；生产只支持
        # PostgreSQL，SQLite则用于完整事务单测。未知测试替身保持无副作用。
        if dialect_name not in {"postgresql", "sqlite"}:
            return
        self.acquire_shared_transaction_lock(db)
        latest = self.repository.get_latest_batch(db)
        if latest is None:
            return
        if latest.status != "COMPLETED":
            raise BusinessRuleError(
                "日终结算尚未完成，当前禁止新下单和新成交",
                error_code="DAILY_SETTLEMENT_TRADING_CLOSED",
            )
        if trading_day is not None and trading_day <= latest.trading_day:
            raise BusinessRuleError(
                "目标交易日已经完成日终结算",
                error_code="TRADING_DAY_ALREADY_SETTLED",
            )
