from dataclasses import dataclass

from app.enums.auth_enums import UserRole
from app.models.app_user import AppUser


@dataclass(frozen=True)
class AccountAccessScope:
    """
    服务端构造的交易账户访问范围。

    ``None``不再表示管理员。普通用户必须携带明确user_id；管理员范围也
    必须通过``admin()``或已认证AppUser显式构造，避免内部调用遗漏参数时
    意外获得无范围权限。
    """

    user_id: str | None
    is_admin: bool
    conceal_resource_existence: bool

    def __post_init__(self) -> None:
        normalized_user_id = (
            self.user_id.strip() if self.user_id is not None else None
        )
        if self.is_admin:
            if normalized_user_id is not None:
                raise ValueError("管理员账户范围不能同时指定user_id")
            if self.conceal_resource_existence:
                raise ValueError("管理员账户范围不能隐藏资源存在性")
        elif not normalized_user_id:
            raise ValueError("普通用户账户范围必须指定user_id")
        object.__setattr__(self, "user_id", normalized_user_id)

    @classmethod
    def for_user(cls, user_id: str) -> "AccountAccessScope":
        """显式创建普通用户范围，统一使用安全404隐藏资源存在性。"""

        return cls(
            user_id=user_id,
            is_admin=False,
            conceal_resource_existence=True,
        )

    @classmethod
    def admin(cls) -> "AccountAccessScope":
        """显式创建管理员或受信任系统调用范围。"""

        return cls(
            user_id=None,
            is_admin=True,
            conceal_resource_existence=False,
        )

    @classmethod
    def from_current_user(cls, user: AppUser) -> "AccountAccessScope":
        """只根据已经认证的服务端用户构造范围，不读取请求体身份字段。"""

        if user.role == UserRole.ADMIN.value:
            return cls.admin()
        return cls.for_user(user.user_id)
