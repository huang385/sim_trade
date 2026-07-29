from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from uuid import uuid4

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from redis.exceptions import RedisError
from sqlalchemy import delete, event, select

from app.api.auth_api import get_auth_service
from app.common.exceptions import AuthenticationError
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
    assert (
        user_client.get(
            f"/api/accounts/{account_b}", headers=headers
        ).status_code
        == 403
    )
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
    assert denied_order.status_code == 403
    assert (
        user_client.get(
            "/api/trades",
            headers=headers,
            params={"account_id": account_b},
        ).status_code
        == 403
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
    assert (
        user_client.get(
            f"/api/orders/{admin_b_order.json()['order_id']}",
            headers=headers,
        ).status_code
        == 403
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
    assert duplicate_b_order.status_code == 403
    assert (
        user_client.post(
            f"/api/orders/{admin_b_order.json()['order_id']}/cancel",
            headers=headers,
            json={"account_id": account_b},
        ).status_code
        == 403
    )
    # 即使把请求体账户伪造成A自己的账户，也必须依据订单真实归属返回403。
    assert (
        user_client.post(
            f"/api/orders/{admin_b_order.json()['order_id']}/cancel",
            headers=headers,
            json={"account_id": account_a},
        ).status_code
        == 403
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
        == 403
    )
    assert (
        user_client.get(
            f"/api/accounts/{account_b}/pnl/realtime",
            headers=headers,
        ).status_code
        == 403
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
