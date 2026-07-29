from types import SimpleNamespace
from unittest.mock import Mock

from app.api.auth_api import get_account_authorization_service
from app.core.security import get_current_user, require_active_user
from app.enums.auth_enums import UserRole, UserStatus
from app.main import app


def install_admin_auth_overrides():
    """为非认证主题的旧API测试注入管理员身份和账户授权桩。"""

    user = SimpleNamespace(
        user_id="U-TEST-ADMIN",
        username="test_admin",
        display_name="测试管理员",
        role=UserRole.ADMIN.value,
        status=UserStatus.ACTIVE.value,
    )
    authorization = Mock()
    authorization.require_account_access.return_value = SimpleNamespace(
        account_id="A001",
        user_id=user.user_id,
    )
    app.dependency_overrides[require_active_user] = lambda: user
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[
        get_account_authorization_service
    ] = lambda: authorization
    return user, authorization
