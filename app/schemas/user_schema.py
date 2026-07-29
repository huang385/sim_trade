from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.enums.auth_enums import UserRole, UserStatus


class UserCreateRequest(BaseModel):
    """管理员创建平台用户时提交的数据。"""

    user_id: str = Field(min_length=1, max_length=64)
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=12, max_length=256)
    display_name: str = Field(min_length=1, max_length=128)
    role: UserRole = UserRole.USER

    @field_validator("user_id", "username", "display_name", mode="before")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return str(value).strip()

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        return value.lower()


class UserStatusUpdateRequest(BaseModel):
    """管理员禁用、启用或解除用户锁定。"""

    status: UserStatus


class UserResponse(BaseModel):
    """不包含密码哈希和登录安全内部状态的用户响应。"""

    model_config = ConfigDict(from_attributes=True)

    user_id: str
    username: str
    display_name: str
    role: UserRole
    status: UserStatus
    last_login_at: datetime | None
    created_at: datetime
    updated_at: datetime


class UserSummary(BaseModel):
    """认证响应中的最小用户身份摘要。"""

    model_config = ConfigDict(from_attributes=True)

    user_id: str
    username: str
    display_name: str
    role: UserRole
