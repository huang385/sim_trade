from datetime import datetime
from decimal import Decimal
from typing import Callable
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.order import Order
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
        account_type: str | None = None,
        fact_reason: str | None = None,
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
                "account_type": account_type or _field(account, "account_type", "FUTURES"),
                "cash_balance": _decimal_string(
                    _field(account, "cash_balance", Decimal("0"))
                ),
                "available_cash": _decimal_string(
                    _field(account, "available_cash", Decimal("0"))
                ),
                "equity": _decimal_string(
                    _field(account, "equity", Decimal("0"))
                ),
                "used_margin": _decimal_string(
                    _field(account, "used_margin", Decimal("0"))
                ),
                "option_used_margin": _decimal_string(
                    _field(account, "option_used_margin", Decimal("0"))
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
                "daily_pnl": _decimal_string(
                    _field(account, "daily_pnl", Decimal("0"))
                ),
                "cumulative_net_pnl": _decimal_string(
                    _field(account, "cumulative_net_pnl", Decimal("0"))
                ),
                # 浮盈、动态权益和风险状态由PnL实时事件负责，数据库事实
                # 事件不得用持久化旧值覆盖这些独立字段域。
                "updated_at": account.updated_at.isoformat(),
                **(
                    {"fact_reason": fact_reason}
                    if fact_reason is not None
                    else {}
                ),
            },
            created_at=occurred_at,
        )

    def create_position_updated(
        self,
        db: Session,
        *,
        position: Position,
        occurred_at: datetime,
        fact_reason: str | None = None,
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
                "instrument_type": position.instrument_type,
                "direction": _field(position, "direction", ""),
                "total_volume": total_volume,
                "today_volume": _field(position, "today_volume", total_volume),
                "yesterday_volume": _field(position, "yesterday_volume", 0),
                "available_volume": _field(position, "available_volume", 0),
                "frozen_volume": _field(position, "frozen_volume", 0),
                "settlement_locked_volume": _field(position, "settlement_locked_volume", 0),
                "average_open_price": _decimal_string(
                    _field(position, "average_open_price", Decimal("0"))
                ),
                "position_cost": _decimal_string(
                    _field(position, "position_cost", Decimal("0"))
                ),
                "used_margin": _decimal_string(
                    _field(position, "used_margin", Decimal("0"))
                ),
                "realtime_required_margin": _decimal_string(
                    _field(
                        position,
                        "realtime_required_margin",
                        Decimal("0"),
                    )
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
                **(
                    {"fact_reason": fact_reason}
                    if fact_reason is not None
                    else {}
                ),
            },
            created_at=occurred_at,
        )

    def create_order_margin_updated(
        self,
        db: Session,
        *,
        order: Order,
        occurred_at: datetime,
    ) -> None:
        """记录活动期权订单保证金和风险状态的完整绝对事实。

        该源事件由现有实时投影链路转换为ORDER_UPDATED。这里只描述订单
        当前绝对状态，客户端不得根据保证金差额自行累加。
        """

        event_id = self.event_id_factory()
        trading_day = _field(order, "trading_day")
        accepted_at = _field(order, "accepted_at")
        cancelled_at = _field(order, "cancelled_at")
        self.repository.create_event(
            db=db,
            event_id=event_id,
            aggregate_type="ORDER",
            aggregate_id=order.order_id,
            event_type="ORDER_MARGIN_UPDATED",
            payload={
                "event_id": event_id,
                "event_type": "ORDER_MARGIN_UPDATED",
                "order_id": order.order_id,
                "client_order_id": _field(order, "client_order_id", ""),
                "account_id": order.account_id,
                "exchange_id": _field(order, "exchange_id", ""),
                "symbol": _field(order, "symbol", ""),
                "order_book_id": _field(order, "order_book_id", ""),
                "trading_day": (
                    trading_day.isoformat() if trading_day is not None else ""
                ),
                "instrument_type": order.instrument_type,
                "direction": _field(order, "direction", ""),
                "offset_flag": _field(order, "offset_flag", ""),
                "order_type": _field(order, "order_type", ""),
                "limit_price": _decimal_string(
                    _field(order, "limit_price", Decimal("0"))
                ),
                "resolved_price": _decimal_string(
                    _field(
                        order,
                        "resolved_price",
                        _field(order, "limit_price", Decimal("0")),
                    )
                ),
                "market_protection_price": (
                    _decimal_string(_field(order, "market_protection_price"))
                    if _field(order, "market_protection_price") is not None
                    else None
                ),
                "total_volume": _field(order, "total_volume", 0),
                "traded_volume": _field(order, "traded_volume", 0),
                "remaining_volume": _field(order, "remaining_volume", 0),
                "cancelled_volume": _field(order, "cancelled_volume", 0),
                "average_price": (
                    _decimal_string(_field(order, "average_price"))
                    if _field(order, "average_price") is not None
                    else None
                ),
                "status": _field(order, "status", ""),
                "submit_status": _field(order, "submit_status", ""),
                "frozen_margin": _decimal_string(
                    _field(order, "frozen_margin", Decimal("0"))
                ),
                "frozen_cash": _decimal_string(
                    _field(order, "frozen_cash", Decimal("0"))
                ),
                "frozen_commission": _decimal_string(
                    _field(order, "frozen_commission", Decimal("0"))
                ),
                "frozen_position_volume": _field(
                    order, "frozen_position_volume", 0
                ),
                "margin_price_mode": _field(order, "margin_price_mode"),
                "margin_underlying_price": (
                    _decimal_string(
                        _field(order, "margin_underlying_price")
                    )
                    if _field(order, "margin_underlying_price") is not None
                    else None
                ),
                "margin_option_price": (
                    _decimal_string(_field(order, "margin_option_price"))
                    if _field(order, "margin_option_price") is not None
                    else None
                ),
                "margin_calculation_version": _field(
                    order, "margin_calculation_version"
                ),
                "margin_risk_state": _field(
                    order, "margin_risk_state", "NORMAL"
                ),
                "accepted_at": (
                    accepted_at.isoformat() if accepted_at is not None else None
                ),
                "cancelled_at": (
                    cancelled_at.isoformat()
                    if cancelled_at is not None
                    else None
                ),
                "updated_at": order.updated_at.isoformat(),
            },
            created_at=occurred_at,
        )
