from pydantic import BaseModel, Field, field_validator

from app.schemas.account_schema import AccountResponse
from app.schemas.user_schema import UserSummary


class LoginRequest(BaseModel):
    """登录请求；密码只在当前请求内使用，不写日志和数据库明文。"""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)

    @field_validator("username", mode="before")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        return str(value).strip().lower()


class MarketSdkGrant(BaseModel):
    """行情SDK直连凭证；仅当登录IP在绑定表中命中时返回。"""

    live_token: str
    data_token: str
    mode: str = "lan"
    live_server_url: str = ""
    data_server_url: str = ""


class TokenResponse(BaseModel):
    """Access Token响应；Refresh Token只写入HttpOnly Cookie。"""

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserSummary
    market_sdk: MarketSdkGrant | None = None


class CurrentUserResponse(BaseModel):
    """当前用户以及其可访问的交易账户概览。"""

    user: UserSummary
    accounts: list[AccountResponse]
