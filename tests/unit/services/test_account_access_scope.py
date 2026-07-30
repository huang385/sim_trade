from types import SimpleNamespace

import pytest

from app.services.account_access_scope import AccountAccessScope


def test_normal_user_scope_requires_explicit_user_id():
    with pytest.raises(ValueError, match="user_id"):
        AccountAccessScope(
            user_id=None,
            is_admin=False,
            conceal_resource_existence=True,
        )
    with pytest.raises(ValueError, match="user_id"):
        AccountAccessScope.for_user("   ")


def test_admin_scope_must_be_explicit_and_cannot_mix_user_identity():
    assert AccountAccessScope.admin() == AccountAccessScope(
        user_id=None,
        is_admin=True,
        conceal_resource_existence=False,
    )
    with pytest.raises(ValueError):
        AccountAccessScope(
            user_id="U001",
            is_admin=True,
            conceal_resource_existence=False,
        )


def test_scope_is_built_only_from_authenticated_server_user():
    normal = AccountAccessScope.from_current_user(
        SimpleNamespace(user_id="U001", role="USER")
    )
    admin = AccountAccessScope.from_current_user(
        SimpleNamespace(user_id="ADMIN", role="ADMIN")
    )

    assert normal == AccountAccessScope.for_user("U001")
    assert admin == AccountAccessScope.admin()
