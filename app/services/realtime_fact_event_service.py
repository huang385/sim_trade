from datetime import datetime
from decimal import Decimal
from typing import Callable
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.position import Position
from app.repositories.outbox_repository import OutboxRepository


def _decimal_string(value: Decimal | None) -> str:
    """实时业务事实中的金额始终使用Decimal字符串。"""

    return format(value or Decimal("0"), "f")


def _field(instance, name: str, default=None):
    """兼容历史对象和精简测试替身；生产模型仍会提供全部字段。"""

    return getattr(instance, name, default)


class RealtimeFactEventService:
    """在业务事务内创建账户和持仓绝对事实Outbox事件。"""

    def __init__(
        self,
        *,
        repository: OutboxRepository,
        event_id_factory: Callable[[], str] | None = None,
    ):
        self.repository = repository
        self.event_id_factory = event_id_factory or (
            lambda: f"EVT-{uuid4().hex.upper()}"
        )

    def create_account_updated(
        self,
        db: Session,
        *,
        account: Account,
        occurred_at: datetime,
        account_id: str | None = None,
    ) -> None:
        """记录提交后的完整账户资金绝对值，客户端无需自行累加。"""

        event_id = self.event_id_factory()
        resolved_account_id = account_id or account.account_id
        self.repository.create_event(
            db=db,
            event_id=event_id,
            aggregate_type="ACCOUNT",
            aggregate_id=resolved_account_id,
            event_type="ACCOUNT_FACT_UPDATED",
            payload={
                "event_id": event_id,
                "event_type": "ACCOUNT_FACT_UPDATED",
                "account_id": resolved_account_id,
                "cash_balance": _decimal_string(
                    _field(account, "cash_balance", Decimal("0"))
                ),
                "used_margin": _decimal_string(
                    _field(account, "used_margin", Decimal("0"))
                ),
                "frozen_margin": _decimal_string(
                    _field(account, "frozen_margin", Decimal("0"))
                ),
                "frozen_cash": _decimal_string(
                    _field(account, "frozen_cash", Decimal("0"))
                ),
                "frozen_commission": _decimal_string(
                    _field(account, "frozen_commission", Decimal("0"))
                ),
                "used_commission": _decimal_string(
                    _field(account, "used_commission", Decimal("0"))
                ),
                "realized_pnl": _decimal_string(
                    _field(account, "realized_pnl", Decimal("0"))
                ),
                "daily_close_pnl": _decimal_string(
                    _field(account, "daily_close_pnl", Decimal("0"))
                ),
                "daily_commission": _decimal_string(
                    _field(account, "daily_commission", Decimal("0"))
                ),
                # 浮盈、动态权益、实时可用资金和风险率由唯一PnL Worker
                # 负责，数据库事实事件不得用较旧估值覆盖该独立字段域。
                "risk_state": _field(account, "risk_state", "NORMAL"),
                "updated_at": account.updated_at.isoformat(),
            },
            created_at=occurred_at,
        )

    def create_position_updated(
        self,
        db: Session,
        *,
        position: Position,
        occurred_at: datetime,
    ) -> None:
        """记录持仓数量、成本和保证金的PostgreSQL提交后绝对事实。"""

        total_volume = _field(
            position,
            "total_volume",
            _field(position, "available_volume", 0)
            + _field(position, "frozen_volume", 0),
        )
        closed = total_volume == 0
        event_type = "POSITION_CLOSED" if closed else "POSITION_UPDATED"
        event_id = self.event_id_factory()
        self.repository.create_event(
            db=db,
            event_id=event_id,
            aggregate_type="POSITION",
            aggregate_id=position.position_id,
            event_type=event_type,
            payload={
                "event_id": event_id,
                "event_type": event_type,
                "position_id": position.position_id,
                "account_id": _field(position, "account_id", ""),
                "exchange_id": _field(position, "exchange_id", ""),
                "symbol": _field(position, "symbol", ""),
                "order_book_id": _field(position, "order_book_id", ""),
                "instrument_type": (
                    _field(position, "instrument_type") or "FUTURES"
                ),
                "direction": _field(position, "direction", ""),
                "total_volume": total_volume,
                "today_volume": _field(position, "today_volume", total_volume),
                "yesterday_volume": _field(position, "yesterday_volume", 0),
                "available_volume": _field(position, "available_volume", 0),
                "frozen_volume": _field(position, "frozen_volume", 0),
                "average_open_price": _decimal_string(
                    _field(position, "average_open_price", Decimal("0"))
                ),
                "position_cost": _decimal_string(
                    _field(position, "position_cost", Decimal("0"))
                ),
                "used_margin": _decimal_string(
                    _field(position, "used_margin", Decimal("0"))
                ),
                "realized_pnl": _decimal_string(
                    _field(position, "realized_pnl", Decimal("0"))
                ),
                "unrealized_pnl": _decimal_string(
                    _field(position, "unrealized_pnl", Decimal("0"))
                ),
                "daily_position_pnl": _decimal_string(
                    _field(position, "daily_position_pnl", Decimal("0"))
                ),
                "daily_close_pnl": _decimal_string(
                    _field(position, "daily_close_pnl", Decimal("0"))
                ),
                "trading_day": (
                    _field(position, "trading_day").isoformat()
                    if _field(position, "trading_day") is not None
                    else ""
                ),
                "updated_at": position.updated_at.isoformat(),
            },
            created_at=occurred_at,
        )
