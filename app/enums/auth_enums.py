from enum import Enum


class UserRole(str, Enum):
    """系统用户角色。第一版只区分普通用户和管理员。"""

    USER = "USER"
    ADMIN = "ADMIN"


class UserStatus(str, Enum):
    """用户登录状态。LOCKED可由管理员解除或在锁定到期后自动恢复。"""

    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    LOCKED = "LOCKED"


class TokenType(str, Enum):
    """JWT用途，防止Access和Refresh Token互相替代。"""

    ACCESS = "access"
    REFRESH = "refresh"
