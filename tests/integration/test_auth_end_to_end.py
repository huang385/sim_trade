from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import logging
from unittest.mock import patch
from uuid import uuid4

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from redis.exceptions import RedisError
from sqlalchemy import delete, event, func, select
from sqlalchemy.exc import OperationalError

from app.api.auth_api import get_auth_service
from app.common.exceptions import AuthenticationError, DataAccessError
from app.common.time_utils import utc_now
from app.core.config import settings
from app.core.database import SessionLocal, engine
from app.core.redis_client import redis_client
from app.core.security import get_token_service
from app.enums.auth_enums import UserRole, UserStatus
from app.main import app
from app.models.account import Account
from app.models.app_user import AppUser
from app.models.auth_refresh_session import AuthRefreshSession
from app.models.order import Order
from app.models.outbox_event import OutboxEvent
from app.models.position import Position
from app.models.trade import Trade
from app.repositories.auth_refresh_session_repository import (
    AuthRefreshSessionRepository,
)
from app.repositories.user_repository import UserRepository
from app.schemas.user_schema import UserCreateRequest
from app.services.admin_user_service import AdminUserService
from app.services.auth_service import AuthService
from app.services.login_rate_limit_service import LoginRateLimitService
from app.services.password_service import PasswordService
from app.services.token_service import TokenService


pytestmark = [pytest.mark.integration, pytest.mark.real_auth]


@dataclass(frozen=True)
class AuthTestContext:
    suffix: str
    admin_id: str
    admin_username: str
    password: str
    service: AuthService
    token_service: TokenService


@pytest.fixture
def auth_context():
    try:
        redis_client.ping()
        with SessionLocal() as db:
            db.execute(select(AppUser.id).limit(1))
    except Exception as exc:
        pytest.skip(f"认证集成依赖不可用: {exc}")

    suffix = uuid4().hex[:10].lower()
    password_service = PasswordService(
        PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)
    )
    token_service = TokenService(
        secret=f"test-auth-secret-{suffix}-0123456789abcdef0123456789",
        issuer="sim-trade-test",
        audience="sim-trade-test-client",
        access_minutes=15,
        refresh_days=7,
    )
    service = AuthService(
        user_repository=UserRepository(),
        refresh_repository=AuthRefreshSessionRepository(),
        password_service=password_service,
        token_service=token_service,
        rate_limit_service=LoginRateLimitService(
            redis_client, limit=100
        ),
    )
    context = AuthTestContext(
        suffix=suffix,
        admin_id=f"UA{suffix.upper()}",
        admin_username=f"admin_{suffix}",
        password="Strong-Integration-Password-123!",
        service=service,
        token_service=token_service,
    )
    with SessionLocal() as db:
        AdminUserService(
            repository=UserRepository(),
            password_service=password_service,
        ).create_user(
            db,
            UserCreateRequest(
                user_id=context.admin_id,
                username=context.admin_username,
                password=context.password,
                display_name="认证集成管理员",
                role=UserRole.ADMIN,
            ),
        )

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_auth_service] = lambda: service
    app.dependency_overrides[get_token_service] = lambda: token_service
    try:
        yield context
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        user_pattern = f"%{suffix.upper()}%"
        with SessionLocal() as db:
            account_ids = list(
                db.scalars(
                    select(Account.account_id).where(
                        Account.user_id.like(user_pattern)
                    )
                )
            )
            order_ids = (
                list(
                    db.scalars(
                        select(Order.order_id).where(
                            Order.account_id.in_(account_ids)
                        )
                    )
                )
                if account_ids
                else []
            )
            if order_ids:
                db.execute(
                    delete(OutboxEvent).where(
                        OutboxEvent.aggregate_id.in_(order_ids)
                    )
                )
                db.execute(
                    delete(Order).where(Order.order_id.in_(order_ids))
                )
            if account_ids:
                db.execute(
                    delete(Account).where(
                        Account.account_id.in_(account_ids)
                    )
                )
            user_ids = list(
                db.scalars(
                    select(AppUser.user_id).where(
                        AppUser.user_id.like(user_pattern)
                    )
                )
            )
            if context.admin_id not in user_ids:
                user_ids.append(context.admin_id)
            db.execute(
                delete(AuthRefreshSession).where(
                    AuthRefreshSession.user_id.in_(user_ids)
                )
            )
            db.execute(
                delete(AppUser).where(AppUser.user_id.in_(user_ids))
            )
            db.commit()
        digest = hashlib.sha256(b"testclient").hexdigest()[:32]
        try:
            redis_client.delete(f"auth:login-rate:{digest}")
        except RedisError:
            pass


def _auth_header(response) -> dict[str, str]:
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create_user(
    client: TestClient,
    headers: dict[str, str],
    *,
    user_id: str,
    username: str,
    password: str,
):
    response = client.post(
        "/api/admin/users",
        headers=headers,
        json={
            "user_id": user_id,
            "username": username,
            "password": password,
            "display_name": username,
            "role": "USER",
        },
    )
    assert response.status_code == 200, response.text


def _create_account(
    client: TestClient,
    headers: dict[str, str],
    *,
    account_id: str,
    user_id: str,
):
    response = client.post(
        "/api/accounts",
        headers=headers,
        json={
            "account_id": account_id,
            "user_id": user_id,
            "account_name": account_id,
            "account_type": "FUTURES",
            "initial_cash": "100000",
        },
    )
    assert response.status_code == 200, response.text


@contextmanager
def _count_sql():
    statements: list[str] = []

    def before_execute(
        _conn, _cursor, statement, _parameters, _context, _executemany
    ):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", before_execute)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", before_execute)


def _selects_from(statements: list[str], table_name: str) -> list[str]:
    """按SQL主FROM表分类，不用脆弱的总语句数量代替性能断言。"""

    marker = f"from {table_name}".lower()
    return [
        statement
        for statement in statements
        if statement.lstrip().lower().startswith("select")
        and marker in " ".join(statement.lower().split())
    ]


def _assert_single_current_user_query(statements: list[str]) -> None:
    user_queries = _selects_from(statements, "app_user")
    assert len(user_queries) == 1
    assert "where app_user.user_id" in " ".join(
        user_queries[0].lower().split()
    )


def test_login_account_authorization_refresh_and_logout(
    auth_context,
    integration_context,
):
    admin_client = TestClient(app)
    admin_login = admin_client.post(
        "/api/auth/login",
        json={
            "username": auth_context.admin_username,
            "password": auth_context.password,
        },
    )
    assert admin_login.status_code == 200
    admin_headers = _auth_header(admin_login)

    user_a_id = f"UAA{auth_context.suffix.upper()}"
    user_b_id = f"UBB{auth_context.suffix.upper()}"
    user_a_name = f"user_a_{auth_context.suffix}"
    user_b_name = f"user_b_{auth_context.suffix}"
    account_a = f"AAA{auth_context.suffix.upper()}"
    account_a_second = f"AAX{auth_context.suffix.upper()}"
    account_b = f"BBB{auth_context.suffix.upper()}"
    _create_user(
        admin_client,
        admin_headers,
        user_id=user_a_id,
        username=user_a_name,
        password=auth_context.password,
    )
    _create_user(
        admin_client,
        admin_headers,
        user_id=user_b_id,
        username=user_b_name,
        password=auth_context.password,
    )
    _create_account(
        admin_client,
        admin_headers,
        account_id=account_a,
        user_id=user_a_id,
    )
    _create_account(
        admin_client,
        admin_headers,
        account_id=account_a_second,
        user_id=user_a_id,
    )
    _create_account(
        admin_client,
        admin_headers,
        account_id=account_b,
        user_id=user_b_id,
    )

    user_client = TestClient(app)
    login = user_client.post(
        "/api/auth/login",
        json={
            "username": user_a_name,
            "password": auth_context.password,
        },
    )
    assert login.status_code == 200
    headers = _auth_header(login)
    old_refresh = user_client.cookies.get(
        settings.auth_refresh_cookie_name
    )
    assert old_refresh
    assert old_refresh not in login.text
    assert (
        user_client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {old_refresh}"},
        ).status_code
        == 401
    )
    wrong_cookie_client = TestClient(app)
    wrong_cookie_client.cookies.set(
        settings.auth_refresh_cookie_name,
        login.json()["access_token"],
        path="/api/auth",
    )
    assert wrong_cookie_client.post("/api/auth/refresh").status_code == 401
    with SessionLocal() as db:
        sessions = list(
            db.scalars(
                select(AuthRefreshSession).where(
                    AuthRefreshSession.user_id == user_a_id
                )
            )
        )
    assert sessions
    assert all(item.token_hash != old_refresh for item in sessions)
    assert all(len(item.token_hash) == 64 for item in sessions)

    with _count_sql() as statements:
        me = user_client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200
    assert [item["account_id"] for item in me.json()["accounts"]] == [
        account_a,
        account_a_second,
    ]
    # 当前用户一次、所属账户列表一次；没有按账户N+1查询。
    assert len(statements) == 2

    with _count_sql() as account_statements:
        own_account = user_client.get(
            f"/api/accounts/{account_a}",
            headers=headers,
        )
    assert own_account.status_code == 200
    # Access Token用户一次、目标账户一次；授权后不再重复查询账户。
    assert len(account_statements) == 2

    with _count_sql() as pnl_statements:
        own_pnl = user_client.get(
            f"/api/accounts/{account_a}/pnl/realtime",
            headers=headers,
        )
    assert own_pnl.status_code == 200
    # Redis无快照回退时也复用授权对象，SQL仍保持用户和账户各一次。
    assert len(pnl_statements) == 2
    foreign_account_response = user_client.get(
        f"/api/accounts/{account_b}",
        headers=headers,
    )
    missing_account_response = user_client.get(
        f"/api/accounts/MISSING{auth_context.suffix.upper()}",
        headers=headers,
    )
    assert foreign_account_response.status_code == 404
    assert missing_account_response.status_code == 404
    assert foreign_account_response.json() == missing_account_response.json()
    denied_order = user_client.post(
        "/api/orders",
        headers=headers,
        json={
            "client_order_id": f"DENY-{auth_context.suffix}",
            "account_id": account_b,
            "exchange_id": integration_context.exchange_id,
            "symbol": integration_context.symbol,
            "direction": "BUY",
            "offset_flag": "OPEN",
            "order_type": "LIMIT",
            "limit_price": "3500",
            "volume": 1,
        },
    )
    assert denied_order.status_code == 404
    missing_account_order = user_client.post(
        "/api/orders",
        headers=headers,
        json={
            "client_order_id": f"MISSING-{auth_context.suffix}",
            "account_id": f"MISSING{auth_context.suffix.upper()}",
            "exchange_id": integration_context.exchange_id,
            "symbol": integration_context.symbol,
            "direction": "BUY",
            "offset_flag": "OPEN",
            "order_type": "LIMIT",
            "limit_price": "3500",
            "volume": 1,
        },
    )
    assert missing_account_order.status_code == 404
    assert missing_account_order.json() == denied_order.json()
    assert (
        user_client.get(
            "/api/trades",
            headers=headers,
            params={"account_id": account_b},
        ).status_code
        == 404
    )
    assert (
        user_client.post(
            "/api/accounts",
            headers=headers,
            json={
                "account_id": f"FORGED{auth_context.suffix.upper()}",
                "user_id": user_b_id,
                "account_name": "伪造账户",
                "account_type": "FUTURES",
                "initial_cash": "100000",
            },
        ).status_code
        == 403
    )

    admin_b_order = admin_client.post(
        "/api/orders",
        headers=admin_headers,
        json={
            "client_order_id": f"ADMIN-B-{auth_context.suffix}",
            "account_id": account_b,
            "exchange_id": integration_context.exchange_id,
            "symbol": integration_context.symbol,
            "direction": "BUY",
            "offset_flag": "OPEN",
            "order_type": "LIMIT",
            "limit_price": "3500",
            "volume": 1,
        },
    )
    assert admin_b_order.status_code == 200
    b_order_id = admin_b_order.json()["order_id"]
    b_trade_id = f"TRB{auth_context.suffix.upper()}"
    b_position_id = f"PSB{auth_context.suffix.upper()}"
    with SessionLocal() as db:
        now = utc_now()
        db.add(
            Trade(
                trade_id=b_trade_id,
                order_id=b_order_id,
                account_id=account_b,
                market_event_id=f"AUTH-ME-{auth_context.suffix}",
                market_stream_message_id="0-1",
                order_book_id=integration_context.symbol,
                exchange_id=integration_context.exchange_id,
                symbol=integration_context.symbol,
                trading_day=integration_context.trading_day,
                direction="BUY",
                offset_flag="OPEN",
                trade_price=Decimal("3500"),
                trade_volume=1,
                turnover=Decimal("35000"),
                margin=Decimal("4200"),
                commission=Decimal("3"),
                realized_pnl=Decimal("0"),
                daily_close_pnl=Decimal("0"),
                trade_time=now,
                created_at=now,
            )
        )
        db.add(
            Position(
                position_id=b_position_id,
                account_id=account_b,
                order_book_id=integration_context.symbol,
                exchange_id=integration_context.exchange_id,
                symbol=integration_context.symbol,
                direction="LONG",
                total_volume=1,
                today_volume=1,
                yesterday_volume=0,
                frozen_volume=0,
                available_volume=1,
                average_open_price=Decimal("3500"),
                position_cost=Decimal("35000"),
                used_margin=Decimal("4200"),
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                daily_position_pnl=Decimal("0"),
                daily_close_pnl=Decimal("0"),
                trading_day=integration_context.trading_day,
                created_at=now,
                updated_at=now,
            )
        )
        db.commit()

    missing_order_response = user_client.get(
        "/api/orders/O-NOT-EXIST",
        headers=headers,
    )
    foreign_order_response = user_client.get(
        f"/api/orders/{b_order_id}",
        headers=headers,
    )
    assert missing_order_response.status_code == 404
    assert foreign_order_response.status_code == 404
    assert foreign_order_response.json() == missing_order_response.json()

    missing_trade_response = user_client.get(
        "/api/trades/T-NOT-EXIST",
        headers=headers,
    )
    foreign_trade_response = user_client.get(
        f"/api/trades/{b_trade_id}",
        headers=headers,
    )
    assert missing_trade_response.status_code == 404
    assert foreign_trade_response.status_code == 404
    assert foreign_trade_response.json() == missing_trade_response.json()
    foreign_allocations_response = user_client.get(
        f"/api/trades/{b_trade_id}/position-allocations",
        headers=headers,
    )
    missing_allocations_response = user_client.get(
        "/api/trades/T-NOT-EXIST/position-allocations",
        headers=headers,
    )
    assert foreign_allocations_response.status_code == 404
    assert (
        foreign_allocations_response.json()
        == missing_allocations_response.json()
    )

    foreign_position_response = user_client.get(
        f"/api/positions/{b_position_id}/pnl/realtime",
        headers=headers,
    )
    missing_position_response = user_client.get(
        "/api/positions/P-NOT-EXIST/pnl/realtime",
        headers=headers,
    )
    assert foreign_position_response.status_code == 404
    assert missing_position_response.status_code == 404
    assert foreign_position_response.json() == missing_position_response.json()

    # 管理员可以访问真实资源，同时对真正不存在的资源仍获得正常404。
    assert (
        admin_client.get(
            f"/api/orders/{b_order_id}",
            headers=admin_headers,
        ).status_code
        == 200
    )
    assert (
        admin_client.get(
            f"/api/trades/{b_trade_id}",
            headers=admin_headers,
        ).status_code
        == 200
    )
    assert (
        admin_client.get(
            f"/api/positions/{b_position_id}/pnl/realtime",
            headers=admin_headers,
        ).status_code
        == 200
    )
    assert (
        admin_client.get(
            "/api/orders/O-NOT-EXIST",
            headers=admin_headers,
        ).status_code
        == 404
    )
    # 即使命中B账户已存在的client_order_id，也必须先拒绝账户越权。
    duplicate_b_order = user_client.post(
        "/api/orders",
        headers=headers,
        json={
            "client_order_id": f"ADMIN-B-{auth_context.suffix}",
            "account_id": account_b,
            "exchange_id": integration_context.exchange_id,
            "symbol": integration_context.symbol,
            "direction": "BUY",
            "offset_flag": "OPEN",
            "order_type": "LIMIT",
            "limit_price": "3500",
            "volume": 1,
        },
    )
    assert duplicate_b_order.status_code == 404
    assert (
        user_client.post(
            f"/api/orders/{b_order_id}/cancel",
            headers=headers,
            json={"account_id": account_b},
        ).status_code
        == 404
    )
    missing_cancel = user_client.post(
        "/api/orders/O-NOT-EXIST/cancel",
        headers=headers,
        json={"account_id": account_a},
    )
    foreign_cancel = user_client.post(
        f"/api/orders/{b_order_id}/cancel",
        headers=headers,
        json={"account_id": account_a},
    )
    assert missing_cancel.status_code == foreign_cancel.status_code == 404
    assert missing_cancel.json() == foreign_cancel.json()
    # 即使把请求体账户伪造成A自己的账户，也按订单真实归属返回安全404。
    assert (
        user_client.post(
            f"/api/orders/{b_order_id}/cancel",
            headers=headers,
            json={"account_id": account_a},
        ).status_code
        == 404
    )

    accepted = user_client.post(
        "/api/orders",
        headers=headers,
        json={
            "client_order_id": f"ALLOW-{auth_context.suffix}",
            "account_id": account_a,
            "exchange_id": integration_context.exchange_id,
            "symbol": integration_context.symbol,
            "direction": "BUY",
            "offset_flag": "OPEN",
            "order_type": "LIMIT",
            "limit_price": "3500",
            "volume": 1,
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert (
        user_client.get(
            f"/api/orders/{accepted.json()['order_id']}",
            headers=headers,
        ).status_code
        == 200
    )
    assert (
        user_client.get(
            "/api/orders",
            headers=headers,
            params={"account_id": account_a},
        ).status_code
        == 200
    )
    assert (
        user_client.get(
            "/api/trades",
            headers=headers,
            params={"account_id": account_a},
        ).status_code
        == 200
    )
    assert (
        user_client.get(
            "/api/positions",
            headers=headers,
            params={"account_id": account_a},
        ).status_code
        == 200
    )
    assert (
        user_client.get(
            "/api/positions",
            headers=headers,
            params={"account_id": account_b},
        ).status_code
        == 404
    )
    assert (
        user_client.get(
            f"/api/accounts/{account_b}/pnl/realtime",
            headers=headers,
        ).status_code
        == 404
    )

    rotated = user_client.post("/api/auth/refresh")
    assert rotated.status_code == 200
    new_refresh = user_client.cookies.get(
        settings.auth_refresh_cookie_name
    )
    assert new_refresh and new_refresh != old_refresh

    replay = TestClient(app)
    replay.cookies.set(
        settings.auth_refresh_cookie_name,
        old_refresh,
        path="/api/auth",
    )
    assert replay.post("/api/auth/refresh").status_code == 401

    assert user_client.post("/api/auth/logout").status_code == 204
    assert user_client.post("/api/auth/logout").status_code == 204
    assert user_client.post("/api/auth/refresh").status_code == 401

    all_accounts = admin_client.get(
        "/api/accounts", headers=admin_headers
    )
    assert all_accounts.status_code == 200
    returned_ids = {item["account_id"] for item in all_accounts.json()}
    assert {account_a, account_a_second, account_b}.issubset(
        returned_ids
    )


def test_unauthorized_trade_requests_do_not_wait_on_foreign_row_locks(
    auth_context,
    integration_context,
):
    """真实行锁验证未授权请求不会等待他人的Account或Order。"""

    admin_client = TestClient(app)
    admin_login = admin_client.post(
        "/api/auth/login",
        json={
            "username": auth_context.admin_username,
            "password": auth_context.password,
        },
    )
    admin_headers = _auth_header(admin_login)
    user_a_id = f"ULA{auth_context.suffix.upper()}"
    user_b_id = f"ULB{auth_context.suffix.upper()}"
    account_a = f"LAA{auth_context.suffix.upper()}"
    account_b = f"LBB{auth_context.suffix.upper()}"
    _create_user(
        admin_client,
        admin_headers,
        user_id=user_a_id,
        username=f"lock_a_{auth_context.suffix}",
        password=auth_context.password,
    )
    _create_user(
        admin_client,
        admin_headers,
        user_id=user_b_id,
        username=f"lock_b_{auth_context.suffix}",
        password=auth_context.password,
    )
    _create_account(
        admin_client,
        admin_headers,
        account_id=account_a,
        user_id=user_a_id,
    )
    _create_account(
        admin_client,
        admin_headers,
        account_id=account_b,
        user_id=user_b_id,
    )
    b_order_response = admin_client.post(
        "/api/orders",
        headers=admin_headers,
        json={
            "client_order_id": f"LOCK-B-{auth_context.suffix}",
            "account_id": account_b,
            "exchange_id": integration_context.exchange_id,
            "symbol": integration_context.symbol,
            "direction": "BUY",
            "offset_flag": "OPEN",
            "order_type": "LIMIT",
            "limit_price": "3500",
            "volume": 1,
        },
    )
    assert b_order_response.status_code == 200
    b_order_id = b_order_response.json()["order_id"]

    user_login = TestClient(app).post(
        "/api/auth/login",
        json={
            "username": f"lock_a_{auth_context.suffix}",
            "password": auth_context.password,
        },
    )
    user_headers = _auth_header(user_login)

    def assert_finishes_without_foreign_lock_wait(call, blocker):
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(call)
            try:
                response = future.result(timeout=2)
            except TimeoutError:
                blocker.rollback()
                future.result(timeout=5)
                pytest.fail(
                    "未授权请求等待了其他用户的业务行锁",
                    pytrace=False,
                )
        assert response.status_code == 404

    with SessionLocal() as blocker:
        blocker.scalar(
            select(Account)
            .where(Account.account_id == account_b)
            .with_for_update()
        )
        assert_finishes_without_foreign_lock_wait(
            lambda: TestClient(app).post(
                "/api/orders",
                headers=user_headers,
                json={
                    "client_order_id": f"LOCK-DENY-{auth_context.suffix}",
                    "account_id": account_b,
                    "exchange_id": integration_context.exchange_id,
                    "symbol": integration_context.symbol,
                    "direction": "BUY",
                    "offset_flag": "OPEN",
                    "order_type": "LIMIT",
                    "limit_price": "3500",
                    "volume": 1,
                },
            ),
            blocker,
        )
        blocker.rollback()

    with SessionLocal() as blocker:
        blocker.scalar(
            select(Order)
            .where(Order.order_id == b_order_id)
            .with_for_update()
        )
        assert_finishes_without_foreign_lock_wait(
            lambda: TestClient(app).post(
                f"/api/orders/{b_order_id}/cancel",
                headers=user_headers,
                json={"account_id": account_a},
            ),
            blocker,
        )
        blocker.rollback()


def test_safe_login_errors_lockout_unlock_and_disabled_user(auth_context):
    admin_service = AdminUserService(
        repository=UserRepository(),
        password_service=auth_context.service.password_service,
    )
    user_id = f"ULK{auth_context.suffix.upper()}"
    username = f"lock_{auth_context.suffix}"
    with SessionLocal() as db:
        admin_service.create_user(
            db,
            UserCreateRequest(
                user_id=user_id,
                username=username,
                password=auth_context.password,
                display_name="锁定测试",
            ),
        )

    client = TestClient(app)
    missing = client.post(
        "/api/auth/login",
        json={
            "username": f"missing_{auth_context.suffix}",
            "password": "wrong",
        },
    )
    wrong = client.post(
        "/api/auth/login",
        json={"username": username, "password": "wrong"},
    )
    assert missing.status_code == wrong.status_code == 401
    assert missing.json()["message"] == wrong.json()["message"]

    for _ in range(settings.auth_max_login_failures - 1):
        response = client.post(
            "/api/auth/login",
            json={"username": username, "password": "wrong"},
        )
        assert response.status_code == 401
    with SessionLocal() as db:
        locked = UserRepository.get_by_user_id(db, user_id)
        assert locked.status == UserStatus.LOCKED.value
        assert locked.failed_login_count == settings.auth_max_login_failures

    assert (
        client.post(
            "/api/auth/login",
            json={
                "username": username,
                "password": auth_context.password,
            },
        ).status_code
        == 401
    )
    with SessionLocal() as db:
        admin_service.update_status(
            db, user_id=user_id, status=UserStatus.ACTIVE
        )
    successful_login = client.post(
            "/api/auth/login",
            json={
                "username": username,
                "password": auth_context.password,
            },
        )
    assert successful_login.status_code == 200
    disabled_refresh_token = client.cookies.get(
        settings.auth_refresh_cookie_name
    )
    assert disabled_refresh_token
    with SessionLocal() as db:
        admin_service.update_status(
            db, user_id=user_id, status=UserStatus.DISABLED
        )
    with SessionLocal() as db:
        active_sessions = db.scalar(
            select(func.count(AuthRefreshSession.id)).where(
                AuthRefreshSession.user_id == user_id,
                AuthRefreshSession.revoked_at.is_(None),
            )
        )
        assert active_sessions == 0
    with SessionLocal() as db:
        with pytest.raises(AuthenticationError) as disabled_refresh:
            auth_context.service.refresh(
                db,
                refresh_token=disabled_refresh_token,
                client_ip="disabled-user",
                user_agent="pytest",
            )
    assert disabled_refresh.value.error_code == "REFRESH_TOKEN_INVALID"
    with SessionLocal() as db:
        admin_service.update_status(
            db, user_id=user_id, status=UserStatus.ACTIVE
        )
    # 重新启用只允许重新登录，禁用前已经撤销的会话不会复活。
    with SessionLocal() as db:
        with pytest.raises(AuthenticationError) as reenabled_refresh:
            auth_context.service.refresh(
                db,
                refresh_token=disabled_refresh_token,
                client_ip="reenabled-user-old-token",
                user_agent="pytest",
            )
    assert reenabled_refresh.value.error_code == "REFRESH_TOKEN_INVALID"
    assert (
        client.post(
            "/api/auth/login",
            json={
                "username": username,
                "password": auth_context.password,
            },
        ).status_code
        == 200
    )

    with SessionLocal() as db:
        admin_service.update_status(
            db, user_id=user_id, status=UserStatus.DISABLED
        )
    assert (
        client.post(
            "/api/auth/login",
            json={
                "username": username,
                "password": auth_context.password,
            },
        ).status_code
        == 401
    )
    assert client.post("/api/auth/refresh").status_code == 401


def test_concurrent_refresh_allows_only_one_success(auth_context):
    user_id = f"UCR{auth_context.suffix.upper()}"
    username = f"concurrent_{auth_context.suffix}"
    with SessionLocal() as db:
        AdminUserService(
            repository=UserRepository(),
            password_service=auth_context.service.password_service,
        ).create_user(
            db,
            UserCreateRequest(
                user_id=user_id,
                username=username,
                password=auth_context.password,
                display_name="并发刷新测试",
            ),
        )
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={
            "username": username,
            "password": auth_context.password,
        },
    )
    refresh_token = client.cookies.get(
        settings.auth_refresh_cookie_name
    )
    assert login.status_code == 200 and refresh_token

    def rotate():
        try:
            with SessionLocal() as db:
                auth_context.service.refresh(
                    db,
                    refresh_token=refresh_token,
                    client_ip="127.0.0.1",
                    user_agent="pytest",
                )
            return "success"
        except AuthenticationError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: rotate(), range(2)))
    assert results.count("success") == 1
    assert results.count("rejected") == 1


def test_password_change_updates_database_and_invalidates_old_login(
    auth_context,
    caplog,
):
    """真实锁定并更新数据库用户，不能用两个独立Hash代替改密流程。"""

    user_id = f"UPW{auth_context.suffix.upper()}"
    username = f"password_{auth_context.suffix}"
    old_password = auth_context.password
    new_password = "Changed-Integration-Password-456!"
    admin_service = AdminUserService(
        repository=UserRepository(),
        password_service=auth_context.service.password_service,
    )
    with SessionLocal() as db:
        created = admin_service.create_user(
            db,
            UserCreateRequest(
                user_id=user_id,
                username=username,
                password=old_password,
                display_name="真实修改密码测试",
            ),
        )
        original_hash = created.password_hash

    # 修改前原密码确实可以通过完整登录流程。
    with SessionLocal() as db:
        before_change = auth_context.service.login(
            db,
            username=username,
            password=old_password,
            client_ip="password-change-before",
            user_agent="pytest",
        )
        old_refresh_token = before_change.tokens.refresh_token

    with caplog.at_level(logging.DEBUG):
        with SessionLocal() as db:
            changed = admin_service.change_password(
                db,
                user_id=user_id,
                new_password=new_password,
            )
            changed_hash = changed.password_hash

        with SessionLocal() as db:
            with pytest.raises(AuthenticationError):
                auth_context.service.login(
                    db,
                    username=username,
                    password=old_password,
                    client_ip="password-change-old",
                    user_agent="pytest",
                )
        with SessionLocal() as db:
            with pytest.raises(AuthenticationError) as old_refresh:
                auth_context.service.refresh(
                    db,
                    refresh_token=old_refresh_token,
                    client_ip="password-change-old-refresh",
                    user_agent="pytest",
                )
            assert old_refresh.value.error_code == "REFRESH_TOKEN_INVALID"
        with SessionLocal() as db:
            new_login = auth_context.service.login(
                db,
                username=username,
                password=new_password,
                client_ip="password-change-new",
                user_agent="pytest",
            )
            assert new_login.tokens.refresh_token != old_refresh_token
        with SessionLocal() as db:
            auth_context.service.refresh(
                db,
                refresh_token=new_login.tokens.refresh_token,
                client_ip="password-change-new-refresh",
                user_agent="pytest",
            )

    assert changed_hash != original_hash
    assert old_password not in caplog.text
    assert new_password not in caplog.text
    assert original_hash not in caplog.text
    assert changed_hash not in caplog.text


def test_password_and_refresh_revocation_roll_back_together_on_db_failure(
    auth_context,
):
    """会话撤销后发生数据库异常时，密码和revoked_at必须一起回滚。"""

    class FailingRefreshRepository(AuthRefreshSessionRepository):
        @staticmethod
        def revoke_active_by_user_id(db, *, user_id, revoked_at):
            AuthRefreshSessionRepository.revoke_active_by_user_id(
                db,
                user_id=user_id,
                revoked_at=revoked_at,
            )
            raise OperationalError(
                "revoke refresh",
                {},
                Exception("forced failure"),
            )

    user_id = f"URB{auth_context.suffix.upper()}"
    username = f"rollback_{auth_context.suffix}"
    with SessionLocal() as db:
        created = AdminUserService(
            repository=UserRepository(),
            password_service=auth_context.service.password_service,
        ).create_user(
            db,
            UserCreateRequest(
                user_id=user_id,
                username=username,
                password=auth_context.password,
                display_name="改密回滚测试",
            ),
        )
        original_hash = created.password_hash
    with SessionLocal() as db:
        login = auth_context.service.login(
            db,
            username=username,
            password=auth_context.password,
            client_ip="password-rollback",
            user_agent="pytest",
        )

    failing_service = AdminUserService(
        repository=UserRepository(),
        password_service=auth_context.service.password_service,
        refresh_repository=FailingRefreshRepository(),
    )
    with SessionLocal() as db:
        with pytest.raises(DataAccessError):
            failing_service.change_password(
                db,
                user_id=user_id,
                new_password="Rollback-New-Password-456!",
            )

    with SessionLocal() as db:
        persisted_user = UserRepository.get_by_user_id(db, user_id)
        refresh_session = (
            AuthRefreshSessionRepository.get_by_jti_for_update(
                db,
                login.tokens.refresh_jti,
            )
        )
        assert persisted_user.password_hash == original_hash
        assert refresh_session.revoked_at is None


def test_authenticated_trading_api_sql_query_shapes(
    auth_context,
    integration_context,
):
    """
    对典型认证请求按表统计SQL。

    重点捕获普通SELECT Account后又SELECT FOR UPDATE的回归，同时允许合约、
    规则、Outbox和响应刷新等合理SQL存在。
    """

    admin_client = TestClient(app)
    admin_login = admin_client.post(
        "/api/auth/login",
        json={
            "username": auth_context.admin_username,
            "password": auth_context.password,
        },
    )
    admin_headers = _auth_header(admin_login)
    user_id = f"UQS{auth_context.suffix.upper()}"
    username = f"query_{auth_context.suffix}"
    account_id = f"QSA{auth_context.suffix.upper()}"
    _create_user(
        admin_client,
        admin_headers,
        user_id=user_id,
        username=username,
        password=auth_context.password,
    )
    _create_account(
        admin_client,
        admin_headers,
        account_id=account_id,
        user_id=user_id,
    )
    user_client = TestClient(app)
    login = user_client.post(
        "/api/auth/login",
        json={
            "username": username,
            "password": auth_context.password,
        },
    )
    headers = _auth_header(login)
    payload = {
        "client_order_id": f"SQL-{auth_context.suffix}-1",
        "account_id": account_id,
        "exchange_id": integration_context.exchange_id,
        "symbol": integration_context.symbol,
        "direction": "BUY",
        "offset_flag": "OPEN",
        "order_type": "LIMIT",
        "limit_price": "3500",
        "volume": 1,
    }

    with patch.object(
        auth_context.token_service,
        "decode",
        wraps=auth_context.token_service.decode,
    ) as decode_token:
        with _count_sql() as create_sql:
            created = user_client.post(
                "/api/orders",
                headers=headers,
                json=payload,
            )
    assert created.status_code == 200, created.text
    assert decode_token.call_count == 1
    _assert_single_current_user_query(create_sql)
    create_accounts = _selects_from(create_sql, "account")
    assert len(create_accounts) == 1
    normalized_create_account = " ".join(
        create_accounts[0].lower().split()
    )
    assert "for update" in normalized_create_account
    assert "and account.user_id =" in normalized_create_account

    with _count_sql() as idempotent_sql:
        repeated = user_client.post(
            "/api/orders",
            headers=headers,
            json=payload,
        )
    assert repeated.status_code == 200
    _assert_single_current_user_query(idempotent_sql)
    idempotent_accounts = _selects_from(idempotent_sql, "account")
    assert len(idempotent_accounts) == 0
    client_id_queries = [
        statement
        for statement in _selects_from(idempotent_sql, "orders")
        if "client_order_id" in statement.lower()
    ]
    assert len(client_id_queries) == 1
    normalized_idempotent = " ".join(
        client_id_queries[0].lower().split()
    )
    assert "join account" in normalized_idempotent
    assert "account.user_id =" in normalized_idempotent
    # 幂等命中在规则读取和账户锁之前返回。
    assert len(_selects_from(idempotent_sql, "instrument")) == 0
    assert len(_selects_from(idempotent_sql, "margin_rule")) == 0
    assert len(_selects_from(idempotent_sql, "fee_rule")) == 0

    admin_payload = dict(payload)
    admin_payload["client_order_id"] = (
        f"SQL-ADMIN-{auth_context.suffix}"
    )
    with _count_sql() as admin_create_sql:
        admin_created = admin_client.post(
            "/api/orders",
            headers=admin_headers,
            json=admin_payload,
        )
    assert admin_created.status_code == 200
    admin_create_accounts = _selects_from(
        admin_create_sql,
        "account",
    )
    assert len(admin_create_accounts) == 1
    normalized_admin_account = " ".join(
        admin_create_accounts[0].lower().split()
    )
    assert "for update" in normalized_admin_account
    assert "and account.user_id =" not in normalized_admin_account

    # 额外创建两条订单，证明列表授权没有按结果逐条查询账户。
    for index in (2, 3):
        next_payload = dict(payload)
        next_payload["client_order_id"] = (
            f"SQL-{auth_context.suffix}-{index}"
        )
        assert (
            user_client.post(
                "/api/orders",
                headers=headers,
                json=next_payload,
            ).status_code
            == 200
        )

    for path in ("/api/orders", "/api/orders/page"):
        with _count_sql() as list_sql:
            response = user_client.get(
                path,
                headers=headers,
                params={"account_id": account_id},
            )
        assert response.status_code == 200
        _assert_single_current_user_query(list_sql)
        assert len(_selects_from(list_sql, "account")) == 1
        assert len(_selects_from(list_sql, "orders")) == 1

    with _count_sql() as trade_sql:
        trades = user_client.get(
            "/api/trades",
            headers=headers,
            params={"account_id": account_id},
        )
    assert trades.status_code == 200
    _assert_single_current_user_query(trade_sql)
    assert len(_selects_from(trade_sql, "account")) == 1
    assert len(_selects_from(trade_sql, "trade")) == 1

    with _count_sql() as position_sql:
        positions = user_client.get(
            "/api/positions",
            headers=headers,
            params={"account_id": account_id},
        )
    assert positions.status_code == 200
    _assert_single_current_user_query(position_sql)
    assert len(_selects_from(position_sql, "account")) == 1
    assert len(_selects_from(position_sql, "position")) == 1

    order_id = created.json()["order_id"]
    with _count_sql() as cancel_sql:
        cancelled = user_client.post(
            f"/api/orders/{order_id}/cancel",
            headers=headers,
            json={"account_id": account_id},
        )
    assert cancelled.status_code == 200
    _assert_single_current_user_query(cancel_sql)
    cancel_accounts = _selects_from(cancel_sql, "account")
    cancel_orders = _selects_from(cancel_sql, "orders")
    assert len(cancel_accounts) == 1
    normalized_cancel_account = " ".join(
        cancel_accounts[0].lower().split()
    )
    assert "for update" in normalized_cancel_account
    assert "and account.user_id =" in normalized_cancel_account
    assert len(
        [item for item in cancel_orders if "for update" in item.lower()]
    ) == 1
    cancel_order_lock = next(
        item for item in cancel_orders if "for update" in item.lower()
    )
    normalized_cancel_order = " ".join(
        cancel_order_lock.lower().split()
    )
    assert "join account" in normalized_cancel_order
    assert "account.user_id =" in normalized_cancel_order
    assert "for update of orders" in normalized_cancel_order

    with _count_sql() as repeat_cancel_sql:
        repeat_cancel = user_client.post(
            f"/api/orders/{order_id}/cancel",
            headers=headers,
            json={"account_id": account_id},
        )
    assert repeat_cancel.status_code == 200
    _assert_single_current_user_query(repeat_cancel_sql)
    repeat_accounts = _selects_from(repeat_cancel_sql, "account")
    repeat_orders = _selects_from(repeat_cancel_sql, "orders")
    assert len(repeat_accounts) == 1
    normalized_repeat_account = " ".join(
        repeat_accounts[0].lower().split()
    )
    assert "for update" in normalized_repeat_account
    assert "and account.user_id =" in normalized_repeat_account
    assert len(
        [item for item in repeat_orders if "for update" in item.lower()]
    ) == 1

    with _count_sql() as admin_users_sql:
        users = admin_client.get(
            "/api/admin/users",
            headers=admin_headers,
        )
    assert users.status_code == 200
    # 一次当前管理员查询、一次用户列表主查询，不按用户加载账户。
    assert len(_selects_from(admin_users_sql, "app_user")) == 2
    assert len(_selects_from(admin_users_sql, "account")) == 0

    with _count_sql() as admin_accounts_sql:
        accounts = admin_client.get(
            "/api/accounts",
            headers=admin_headers,
        )
    assert accounts.status_code == 200
    _assert_single_current_user_query(admin_accounts_sql)
    assert len(_selects_from(admin_accounts_sql, "account")) == 1
