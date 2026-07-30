from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from redis.exceptions import RedisError
from sqlalchemy import delete, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.core.database import SessionLocal
from app.core.redis_client import redis_client
from app.infrastructure.redis_keys import (
    PNL_DIRTY_CONTRACTS_KEY,
    PNL_DIRTY_CONTRACT_VERSIONS_KEY,
    pnl_dirty_contract_accounts_key,
    pnl_dirty_contract_member,
)
from app.models.account import Account
from app.models.fee_rule import FeeRule
from app.models.instrument import Instrument
from app.models.margin_rule import MarginRule
from app.models.order import Order
from app.models.outbox_event import OutboxEvent
from app.models.trade import Trade
from app.models.position import Position
from app.models.position_detail import PositionDetail
from app.models.position_freeze_allocation import PositionFreezeAllocation
from app.models.trade_position_allocation import TradePositionAllocation
from app.models.app_user import AppUser
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
from app.services.account_authorization_service import (
    AccountAuthorizationService,
)
from app.api.auth_api import get_account_authorization_service
from app.main import app
from tests.api_auth_helpers import install_admin_auth_overrides


@dataclass(frozen=True)
class IntegrationContext:
    """每个集成测试独享的账户、合约和交易规则编号。"""

    account_id: str
    user_id: str
    exchange_id: str
    symbol: str
    trading_day: date


@pytest.fixture(autouse=True)
def integration_api_auth(request):
    """
    既有集成测试关注交易链路，默认注入管理员身份。

    新增的真实认证测试使用real_auth标记，必须走JWT和数据库用户查询，
    不能被该兼容夹具绕过。
    """

    previous = dict(app.dependency_overrides)
    if request.node.get_closest_marker("real_auth") is None:
        install_admin_auth_overrides()
        # 只替换身份为管理员，授权服务仍使用真实仓储。这样资源ID授权、
        # 已锁定Account复用和防枚举逻辑都能在旧交易集成测试中实际执行。
        authorization = AccountAuthorizationService()
        app.dependency_overrides[
            get_account_authorization_service
        ] = lambda: authorization
    try:
        yield
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.fixture
def integration_context():
    suffix = uuid4().hex[:10].upper()
    context = IntegrationContext(
        account_id=f"ITA{suffix}",
        user_id=f"ITU{suffix}",
        exchange_id="ITEX",
        symbol=f"IT{suffix}",
        trading_day=date.today(),
    )

    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
            db.add(
                AppUser(
                    user_id=context.user_id,
                    username=f"it_{suffix.lower()}",
                    password_hash="!integration-no-login!",
                    display_name="集成测试用户",
                    role="USER",
                    status="ACTIVE",
                )
            )
            # 未定义ORM relationship时显式flush，确保账户外键目标先落库。
            db.flush()
            db.add_all(
                [
                    Account(
                        account_id=context.account_id,
                        user_id=context.user_id,
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
                delete(TradePositionAllocation).where(
                    TradePositionAllocation.trade_id.in_(trade_ids)
                )
            )
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
                delete(PositionFreezeAllocation).where(
                    PositionFreezeAllocation.order_id.in_(order_ids)
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
        db.execute(
            delete(AppUser).where(AppUser.user_id == context.user_id)
        )
        db.commit()

    # 集成测试可能在外部Worker同时运行时发布TRADE_CREATED。数据库测试账户
    # 删除后必须精确清理它对应的PnL Dirty引用，避免测试残留在后续实时
    # Worker中被当成失效账户反复重试。随机ITEX合约不会与用户数据重合。
    member = pnl_dirty_contract_member(
        context.exchange_id,
        context.symbol,
    )
    try:
        redis_client.srem(PNL_DIRTY_CONTRACTS_KEY, member)
        redis_client.hdel(PNL_DIRTY_CONTRACT_VERSIONS_KEY, member)
        redis_client.delete(
            pnl_dirty_contract_accounts_key(
                context.exchange_id,
                context.symbol,
            )
        )
    except RedisError:
        # Redis不可用不应把只依赖PostgreSQL的测试改判失败；需要Redis的
        # 测试会在自身初始化阶段按项目规范明确skip。
        pass


def make_request(
    context: IntegrationContext,
    *,
    client_order_id: str,
    direction: str = "BUY",
    offset_flag: str = "OPEN",
    limit_price: Decimal = Decimal("3500"),
    volume: int = 2,
) -> OrderCreateRequest:
    """构造使用当前集成测试参考数据的订单请求。"""

    return OrderCreateRequest(
        client_order_id=client_order_id,
        account_id=context.account_id,
        exchange_id=context.exchange_id,
        symbol=context.symbol,
        direction=direction,
        offset_flag=offset_flag,
        order_type="LIMIT",
        limit_price=limit_price,
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
