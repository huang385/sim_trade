from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.core.database import SessionLocal
from app.models.account import Account
from app.models.fee_rule import FeeRule
from app.models.instrument import Instrument
from app.models.margin_rule import MarginRule
from app.models.order import Order
from app.models.outbox_event import OutboxEvent
from app.models.trade import Trade
from app.models.position import Position
from app.models.position_detail import PositionDetail
from app.repositories.account_repository import AccountRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.outbox_repository import OutboxRepository
from app.schemas.order_schema import OrderCreateRequest
from app.services.fee_calculator import FeeCalculator
from app.services.margin_calculator import MarginCalculator
from app.services.order_freeze_service import OrderFreezeService
from app.services.order_cancellation_service import OrderCancellationService
from app.services.order_service import OrderService
from app.services.order_validation_service import OrderValidationService
from app.services.rule_query_service import get_rule_query_service


@dataclass(frozen=True)
class IntegrationContext:
    """每个集成测试独享的账户、合约和交易规则编号。"""

    account_id: str
    exchange_id: str
    symbol: str
    trading_day: date


@pytest.fixture
def integration_context():
    suffix = uuid4().hex[:10].upper()
    context = IntegrationContext(
        account_id=f"ITA{suffix}",
        exchange_id="ITEX",
        symbol=f"IT{suffix}",
        trading_day=date.today(),
    )

    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
            db.add_all(
                [
                    Account(
                        account_id=context.account_id,
                        user_id=None,
                        account_name="订单集成测试账户",
                        account_type="FUTURES",
                        initial_cash=Decimal("100000"),
                        cash_balance=Decimal("100000"),
                        available_cash=Decimal("100000"),
                        frozen_cash=Decimal("0"),
                        equity=Decimal("100000"),
                        used_margin=Decimal("0"),
                        frozen_margin=Decimal("0"),
                        realized_pnl=Decimal("0"),
                        unrealized_pnl=Decimal("0"),
                        daily_pnl=Decimal("0"),
                        used_commission=Decimal("0"),
                        frozen_commission=Decimal("0"),
                        risk_ratio=Decimal("0"),
                        status="NORMAL",
                        trading_day=context.trading_day,
                    ),
                    Instrument(
                        order_book_id=context.symbol,
                        symbol=context.symbol,
                        exchange_id=context.exchange_id,
                        instrument_name="集成测试合约",
                        product_id="IT",
                        market_type="FUTURES",
                        contract_multiplier=Decimal("10"),
                        price_tick=Decimal("1"),
                        min_volume=1,
                        max_volume=100,
                        is_active=True,
                        data_source="INTERNAL",
                    ),
                    MarginRule(
                        order_book_id=context.symbol,
                        symbol=context.symbol,
                        exchange_id=context.exchange_id,
                        trading_day=context.trading_day,
                        long_margin_rate=Decimal("0.12"),
                        short_margin_rate=Decimal("0.13"),
                        min_margin_rate=None,
                        data_source="INTERNAL",
                    ),
                    FeeRule(
                        order_book_id=context.symbol,
                        symbol=context.symbol,
                        exchange_id=context.exchange_id,
                        trading_day=context.trading_day,
                        commission_type="BY_VOLUME",
                        open_commission=Decimal("3"),
                        close_commission=Decimal("3"),
                        close_today_commission=Decimal("6"),
                        discount_rate=None,
                        data_source="INTERNAL",
                    ),
                ]
            )
            db.commit()
    except SQLAlchemyError as exc:
        pytest.skip(f"PostgreSQL不可用或尚未执行迁移: {exc}")

    yield context

    # 只删除该测试生成的精确编号数据，不影响用户已有账户、规则和订单。
    with SessionLocal() as db:
        order_ids = db.scalars(
            select(Order.order_id).where(
                Order.account_id == context.account_id
            )
        ).all()
        trade_ids = db.scalars(
            select(Trade.trade_id).where(
                Trade.account_id == context.account_id
            )
        ).all()
        if trade_ids:
            db.execute(
                delete(OutboxEvent).where(
                    OutboxEvent.aggregate_type == "TRADE",
                    OutboxEvent.aggregate_id.in_(trade_ids),
                )
            )
        if order_ids:
            db.execute(
                delete(OutboxEvent).where(
                    OutboxEvent.aggregate_type == "ORDER",
                    OutboxEvent.aggregate_id.in_(order_ids),
                )
            )
        db.execute(
            delete(PositionDetail).where(
                PositionDetail.account_id == context.account_id
            )
        )
        db.execute(
            delete(Position).where(Position.account_id == context.account_id)
        )
        db.execute(delete(Trade).where(Trade.account_id == context.account_id))
        db.execute(delete(Order).where(Order.account_id == context.account_id))
        db.execute(
            delete(FeeRule).where(
                FeeRule.exchange_id == context.exchange_id,
                FeeRule.symbol == context.symbol,
            )
        )
        db.execute(
            delete(MarginRule).where(
                MarginRule.exchange_id == context.exchange_id,
                MarginRule.symbol == context.symbol,
            )
        )
        db.execute(
            delete(Instrument).where(
                Instrument.exchange_id == context.exchange_id,
                Instrument.symbol == context.symbol,
            )
        )
        db.execute(
            delete(Account).where(Account.account_id == context.account_id)
        )
        db.commit()


def make_request(
    context: IntegrationContext,
    *,
    client_order_id: str,
    direction: str = "BUY",
    volume: int = 2,
) -> OrderCreateRequest:
    """构造使用当前集成测试参考数据的订单请求。"""

    return OrderCreateRequest(
        client_order_id=client_order_id,
        account_id=context.account_id,
        exchange_id=context.exchange_id,
        symbol=context.symbol,
        direction=direction,
        offset_flag="OPEN",
        order_type="LIMIT",
        limit_price=Decimal("3500"),
        volume=volume,
    )


def make_order_service(
    context: IntegrationContext,
    *,
    outbox_repository=None,
    event_id_factory=None,
) -> OrderService:
    """构造使用真实 PostgreSQL 仓储的订单服务。"""

    kwargs = {}
    if event_id_factory is not None:
        kwargs["event_id_factory"] = event_id_factory
    return OrderService(
        order_repository=OrderRepository(),
        account_repository=AccountRepository(),
        rule_query_service=get_rule_query_service(),
        validation_service=OrderValidationService(),
        freeze_service=OrderFreezeService(),
        margin_calculator=MarginCalculator(),
        fee_calculator=FeeCalculator(),
        outbox_repository=outbox_repository or OutboxRepository(),
        trading_day_provider=lambda: context.trading_day,
        **kwargs,
    )


def make_cancellation_service(
    *,
    outbox_repository=None,
    event_id_factory=None,
) -> OrderCancellationService:
    """构造使用真实 PostgreSQL 行锁和仓储的撤单事务服务。"""

    kwargs = {}
    if event_id_factory is not None:
        kwargs["event_id_factory"] = event_id_factory
    return OrderCancellationService(
        order_repository=OrderRepository(),
        account_repository=AccountRepository(),
        freeze_service=OrderFreezeService(),
        outbox_repository=outbox_repository or OutboxRepository(),
        **kwargs,
    )
