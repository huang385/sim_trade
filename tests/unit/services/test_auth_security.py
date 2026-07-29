from datetime import timedelta
import logging

import pytest
from argon2 import PasswordHasher

from app.common.exceptions import AuthenticationError
from app.common.time_utils import utc_now
from app.enums.auth_enums import TokenType
from app.services.password_service import PasswordService
from app.services.token_service import TokenService


def _password_service() -> PasswordService:
    """测试降低Argon2资源参数，只缩短测试时间，不改变生产默认参数。"""

    return PasswordService(
        PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)
    )


def _token_service(secret: str = "s" * 48) -> TokenService:
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


def test_password_change_invalidates_old_password():
    service = _password_service()
    old_hash = service.hash_password("Old-Password-123!")
    new_hash = service.hash_password("New-Password-456!")

    assert service.verify_password(old_hash, "Old-Password-123!")
    assert not service.verify_password(new_hash, "Old-Password-123!")
    assert service.verify_password(new_hash, "New-Password-456!")


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
        _token_service("x" * 48).decode(
            valid.access_token, expected_type=TokenType.ACCESS
        )


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
