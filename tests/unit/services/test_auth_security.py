from datetime import timedelta
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from argon2 import PasswordHasher
from sqlalchemy.exc import OperationalError

from app.common.exceptions import (
    AuthenticationError,
    DataAccessError,
    RateLimitError,
    ServiceUnavailableError,
)
from app.common.time_utils import utc_now
from app.core.config import Settings
from app.enums.auth_enums import TokenType, UserStatus
from app.repositories.auth_refresh_session_repository import (
    AuthRefreshSessionRepository,
)
from app.repositories.user_repository import UserRepository
from app.services.admin_user_service import AdminUserService
from app.services.auth_service import AuthService
from app.services.login_rate_limit_service import LoginRateLimitService
from app.services.password_service import PasswordService
from app.services.token_service import TokenService


def _password_service() -> PasswordService:
    """测试降低Argon2资源参数，只缩短测试时间，不改变生产默认参数。"""

    return PasswordService(
        PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)
    )


VALID_TEST_SECRET = (
    "D7v!qP2m#L9x@R4k$T8n%W3c&Y6h*B1s-F5j_Z0u+G2a"
)
OTHER_VALID_TEST_SECRET = (
    "K4z@N8p!C2w#H7m$Q5r%V1x&S9b*D6t-J3f_A0y+L8e"
)


def _token_service(secret: str = VALID_TEST_SECRET) -> TokenService:
    return TokenService(
        secret=secret,
        issuer="test-issuer",
        audience="test-audience",
        access_minutes=15,
        refresh_days=7,
    )


def test_password_uses_argon2id_and_never_stores_plaintext():
    service = _password_service()
    password = "Strong-Test-Password-123!"

    password_hash = service.hash_password(password)

    assert password_hash != password
    assert password_hash.startswith("$argon2id$")
    assert service.verify_password(password_hash, password) is True
    assert service.verify_password(password_hash, "wrong-password") is False


def test_password_verify_fails_fast_when_all_slots_are_busy():
    service = PasswordService(
        PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1),
        max_verify_concurrency=1,
        verify_acquire_timeout_seconds=0,
    )
    assert service._verify_slots.acquire(blocking=False) is True
    try:
        with pytest.raises(RateLimitError) as exc_info:
            service.verify_password("invalid-hash", "password")
    finally:
        service._verify_slots.release()

    assert exc_info.value.error_code == "PASSWORD_VERIFY_BUSY"


def test_access_and_refresh_tokens_have_strict_separate_types():
    service = _token_service()
    pair = service.create_pair("U001")

    access = service.decode(
        pair.access_token, expected_type=TokenType.ACCESS
    )
    refresh = service.decode(
        pair.refresh_token, expected_type=TokenType.REFRESH
    )

    assert access.user_id == refresh.user_id == "U001"
    assert access.jti != refresh.jti
    with pytest.raises(AuthenticationError):
        service.decode(
            pair.refresh_token, expected_type=TokenType.ACCESS
        )
    with pytest.raises(AuthenticationError):
        service.decode(
            pair.access_token, expected_type=TokenType.REFRESH
        )


def test_expired_and_wrong_signature_tokens_are_rejected():
    service = _token_service()
    expired = service.create_pair(
        "U001", now=utc_now() - timedelta(minutes=16)
    )
    valid = service.create_pair("U001")

    with pytest.raises(AuthenticationError):
        service.decode(
            expired.access_token, expected_type=TokenType.ACCESS
        )
    with pytest.raises(AuthenticationError):
        _token_service(OTHER_VALID_TEST_SECRET).decode(
            valid.access_token, expected_type=TokenType.ACCESS
        )
    expired_refresh = service.create_pair(
        "U001",
        now=utc_now() - timedelta(days=8),
    )
    with pytest.raises(AuthenticationError):
        service.decode(
            expired_refresh.refresh_token,
            expected_type=TokenType.REFRESH,
        )


@pytest.mark.parametrize(
    "unsafe_secret",
    [
        "",
        "too-short",
        "replace-with-at-least-32-random-bytes",
        "a" * 64,
        "abab" * 16,
    ],
)
def test_unsafe_or_public_jwt_secret_cannot_sign_tokens(unsafe_secret):
    service = _token_service(unsafe_secret)

    with pytest.raises(ServiceUnavailableError) as exc_info:
        service.create_pair("U001")

    assert exc_info.value.error_code == "AUTH_NOT_CONFIGURED"
    if unsafe_secret:
        assert unsafe_secret not in str(exc_info.value)


def test_qualified_jwt_secret_can_sign_and_verify_tokens():
    service = _token_service()

    pair = service.create_pair("U001")
    claims = service.decode(
        pair.access_token,
        expected_type=TokenType.ACCESS,
    )

    assert claims.user_id == "U001"


def test_env_example_does_not_publish_an_acceptable_jwt_secret():
    project_root = Path(__file__).resolve().parents[3]
    lines = (project_root / ".env.example").read_text(
        encoding="utf-8"
    ).splitlines()

    assert "AUTH_JWT_SECRET=" in lines
    assert not any(
        line.startswith("AUTH_JWT_SECRET=")
        and line != "AUTH_JWT_SECRET="
        for line in lines
    )


def test_production_requires_safe_secret_and_secure_refresh_cookie():
    insecure_cookie = Settings(
        _env_file=None,
        app_env="prod",
        debug=False,
        auth_jwt_secret=VALID_TEST_SECRET,
        auth_refresh_cookie_secure=False,
    )
    unsafe_secret = Settings(
        _env_file=None,
        app_env="prod",
        debug=False,
        auth_jwt_secret="replace-with-at-least-32-random-bytes",
        auth_refresh_cookie_secure=True,
    )
    secure = Settings(
        _env_file=None,
        app_env="prod",
        debug=False,
        auth_jwt_secret=VALID_TEST_SECRET,
        auth_refresh_cookie_secure=True,
    )

    with pytest.raises(ValueError, match="Refresh Cookie"):
        insecure_cookie.validate_runtime_security()
    with pytest.raises(ValueError, match="JWT"):
        unsafe_secret.validate_runtime_security()
    secure.validate_runtime_security()


def test_production_requires_debug_disabled_without_exposing_secret():
    production_debug = Settings(
        _env_file=None,
        app_env="production",
        debug=True,
        auth_jwt_secret=VALID_TEST_SECRET,
        auth_refresh_cookie_secure=True,
    )
    development_debug = Settings(
        _env_file=None,
        app_env="dev",
        debug=True,
        auth_jwt_secret="",
        auth_refresh_cookie_secure=False,
    )

    with pytest.raises(ValueError, match="Debug") as exc_info:
        production_debug.validate_runtime_security()
    assert VALID_TEST_SECRET not in str(exc_info.value)
    development_debug.validate_runtime_security()


def test_admin_password_change_commits_only_in_service():
    repository = Mock(spec=UserRepository)
    refresh_repository = Mock(spec=AuthRefreshSessionRepository)
    user = SimpleNamespace(
        user_id="U001",
        password_hash="old-hash",
        password_changed_at=None,
    )
    repository.get_by_user_id_for_update.return_value = user
    password_service = Mock(spec=PasswordService)
    password_service.hash_password.return_value = "new-argon2-hash"
    service = AdminUserService(
        repository=repository,
        password_service=password_service,
        refresh_repository=refresh_repository,
    )
    db = Mock()

    result = service.change_password(
        db,
        user_id=" U001 ",
        new_password="New-Password-456!",
    )

    assert result is user
    assert user.password_hash == "new-argon2-hash"
    assert user.password_changed_at is not None
    refresh_repository.revoke_active_by_user_id.assert_called_once_with(
        db,
        user_id="U001",
        revoked_at=user.password_changed_at,
    )
    repository.get_by_user_id_for_update.assert_called_once_with(
        db,
        "U001",
    )
    # Repository接口没有事务方法，提交和刷新只发生在Service持有的Session。
    assert not hasattr(repository, "commit")
    assert not hasattr(repository, "rollback")
    db.commit.assert_called_once()
    db.refresh.assert_called_once_with(user)


def test_password_change_rolls_back_when_refresh_revocation_fails():
    repository = Mock(spec=UserRepository)
    user = SimpleNamespace(
        user_id="U001",
        password_hash="old-hash",
        password_changed_at=None,
    )
    repository.get_by_user_id_for_update.return_value = user
    password_service = Mock(spec=PasswordService)
    password_service.hash_password.return_value = "new-argon2-hash"
    refresh_repository = Mock(spec=AuthRefreshSessionRepository)
    refresh_repository.revoke_active_by_user_id.side_effect = (
        OperationalError("revoke", {}, Exception("failed"))
    )
    service = AdminUserService(
        repository=repository,
        password_service=password_service,
        refresh_repository=refresh_repository,
    )
    db = Mock()

    with pytest.raises(DataAccessError):
        service.change_password(
            db,
            user_id="U001",
            new_password="New-Password-456!",
        )

    db.commit.assert_not_called()
    db.rollback.assert_called_once()


def test_disabling_user_revokes_sessions_but_reenabling_does_not_restore_them():
    repository = Mock(spec=UserRepository)
    user = SimpleNamespace(
        user_id="U001",
        status=UserStatus.ACTIVE.value,
        failed_login_count=0,
        locked_until=None,
    )
    repository.get_by_user_id_for_update.return_value = user
    refresh_repository = Mock(spec=AuthRefreshSessionRepository)
    service = AdminUserService(
        repository=repository,
        password_service=Mock(spec=PasswordService),
        refresh_repository=refresh_repository,
    )
    db = Mock()

    service.update_status(
        db,
        user_id="U001",
        status=UserStatus.DISABLED,
    )
    revoke_call = refresh_repository.revoke_active_by_user_id.call_args
    assert revoke_call.kwargs["user_id"] == "U001"
    assert revoke_call.kwargs["revoked_at"] is not None

    refresh_repository.reset_mock()
    service.update_status(
        db,
        user_id="U001",
        status=UserStatus.ACTIVE,
    )
    refresh_repository.revoke_active_by_user_id.assert_not_called()
    assert user.status == UserStatus.ACTIVE.value


def test_refresh_locks_user_before_refresh_session():
    calls: list[str] = []
    token_service = _token_service()
    pair = token_service.create_pair("U001")
    user = SimpleNamespace(
        user_id="U001",
        status=UserStatus.ACTIVE.value,
    )
    refresh_session = SimpleNamespace(
        user_id="U001",
        token_hash=token_service.hash_refresh_token(pair.refresh_token),
        expires_at=pair.refresh_expires_at,
        revoked_at=None,
        last_used_at=None,
        replaced_by_jti=None,
    )
    user_repository = Mock(spec=UserRepository)
    refresh_repository = Mock(spec=AuthRefreshSessionRepository)
    user_repository.get_by_user_id_for_update.side_effect = (
        lambda *_args: calls.append("USER") or user
    )
    refresh_repository.get_by_jti_for_update.side_effect = (
        lambda *_args: calls.append("REFRESH_SESSION")
        or refresh_session
    )
    service = AuthService(
        user_repository=user_repository,
        refresh_repository=refresh_repository,
        password_service=Mock(spec=PasswordService),
        token_service=token_service,
        rate_limit_service=Mock(spec=LoginRateLimitService),
    )
    db = Mock()

    service.refresh(
        db,
        refresh_token=pair.refresh_token,
        client_ip="127.0.0.1",
        user_agent="pytest",
    )

    assert calls == ["USER", "REFRESH_SESSION"]
    db.commit.assert_called_once()


def _refresh_test_objects():
    token_service = _token_service()
    pair = token_service.create_pair("U001")
    user = SimpleNamespace(
        user_id="U001",
        status=UserStatus.ACTIVE.value,
    )
    refresh_session = SimpleNamespace(
        user_id="U001",
        token_hash=token_service.hash_refresh_token(pair.refresh_token),
        expires_at=pair.refresh_expires_at,
        revoked_at=None,
        last_used_at=None,
        replaced_by_jti=None,
    )
    user_repository = Mock(spec=UserRepository)
    user_repository.get_by_user_id_for_update.return_value = user
    refresh_repository = Mock(spec=AuthRefreshSessionRepository)
    refresh_repository.get_by_jti_for_update.return_value = refresh_session
    service = AuthService(
        user_repository=user_repository,
        refresh_repository=refresh_repository,
        password_service=Mock(spec=PasswordService),
        token_service=token_service,
        rate_limit_service=Mock(spec=LoginRateLimitService),
    )
    return SimpleNamespace(
        pair=pair,
        user=user,
        session=refresh_session,
        user_repository=user_repository,
        refresh_repository=refresh_repository,
        service=service,
        db=Mock(),
    )


@pytest.mark.parametrize("invalid_user", [None, "DISABLED", "LOCKED"])
def test_refresh_rejects_missing_or_inactive_user_before_session_lock(
    invalid_user,
):
    context = _refresh_test_objects()
    context.user_repository.get_by_user_id_for_update.return_value = (
        None
        if invalid_user is None
        else SimpleNamespace(user_id="U001", status=invalid_user)
    )

    with pytest.raises(AuthenticationError) as exc_info:
        context.service.refresh(
            context.db,
            refresh_token=context.pair.refresh_token,
            client_ip="127.0.0.1",
            user_agent="pytest",
        )

    assert exc_info.value.error_code == "REFRESH_TOKEN_INVALID"
    context.refresh_repository.get_by_jti_for_update.assert_not_called()
    context.db.rollback.assert_called_once()


@pytest.mark.parametrize(
    "invalid_session",
    [
        None,
        {"user_id": "U-OTHER"},
        {"token_hash": "wrong-token-hash"},
        {"revoked_at": utc_now()},
        {"expires_at": utc_now() - timedelta(seconds=1)},
    ],
)
def test_refresh_rejects_missing_mismatched_revoked_or_expired_session(
    invalid_session,
):
    context = _refresh_test_objects()
    if invalid_session is None:
        session = None
    else:
        for field, value in invalid_session.items():
            setattr(context.session, field, value)
        session = context.session
    context.refresh_repository.get_by_jti_for_update.return_value = session

    with pytest.raises(AuthenticationError) as exc_info:
        context.service.refresh(
            context.db,
            refresh_token=context.pair.refresh_token,
            client_ip="127.0.0.1",
            user_agent="pytest",
        )

    assert exc_info.value.error_code == "REFRESH_TOKEN_INVALID"
    context.refresh_repository.add.assert_not_called()
    context.db.commit.assert_not_called()
    context.db.rollback.assert_called_once()


def test_same_refresh_token_can_only_be_rotated_once():
    context = _refresh_test_objects()

    first = context.service.refresh(
        context.db,
        refresh_token=context.pair.refresh_token,
        client_ip="127.0.0.1",
        user_agent="pytest",
    )
    with pytest.raises(AuthenticationError):
        context.service.refresh(
            context.db,
            refresh_token=context.pair.refresh_token,
            client_ip="127.0.0.1",
            user_agent="pytest",
        )

    assert first.tokens.refresh_token != context.pair.refresh_token
    context.refresh_repository.add.assert_called_once()
    context.db.commit.assert_called_once()
    context.db.rollback.assert_called_once()


def test_refresh_database_failure_rolls_back_rotation():
    context = _refresh_test_objects()
    context.refresh_repository.add.side_effect = OperationalError(
        "insert refresh",
        {},
        Exception("failed"),
    )

    with pytest.raises(DataAccessError):
        context.service.refresh(
            context.db,
            refresh_token=context.pair.refresh_token,
            client_ip="127.0.0.1",
            user_agent="pytest",
        )

    context.db.commit.assert_not_called()
    context.db.rollback.assert_called_once()


def test_refresh_repository_has_no_transaction_control_methods():
    assert not hasattr(AuthRefreshSessionRepository, "commit")
    assert not hasattr(AuthRefreshSessionRepository, "rollback")


def test_password_and_tokens_are_not_logged(caplog):
    password_service = _password_service()
    token_service = _token_service()
    password = "Never-Log-This-Password!"

    with caplog.at_level(logging.DEBUG):
        password_hash = password_service.hash_password(password)
        pair = token_service.create_pair("U001")
        password_service.verify_password(password_hash, password)
        token_service.decode(
            pair.access_token, expected_type=TokenType.ACCESS
        )

    log_text = caplog.text
    assert password not in log_text
    assert password_hash not in log_text
    assert pair.access_token not in log_text
    assert pair.refresh_token not in log_text
