from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
from typing import Any
from uuid import uuid4

import jwt

from app.common.exceptions import AuthenticationError, ServiceUnavailableError
from app.common.time_utils import utc_now
from app.core.config import is_unsafe_auth_jwt_secret, settings
from app.enums.auth_enums import TokenType


@dataclass(frozen=True)
class TokenClaims:
    """经过完整签名、期限、Issuer和Audience校验的JWT身份。"""

    user_id: str
    jti: str
    token_type: TokenType
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class TokenPair:
    """一次登录或刷新生成的短期Access和可轮换Refresh Token。"""

    access_token: str
    refresh_token: str
    access_expires_in: int
    refresh_jti: str
    refresh_expires_at: datetime


class TokenService:
    """只负责JWT签发和严格验证，不访问数据库。"""

    def __init__(
        self,
        *,
        secret: str | None = None,
        algorithm: str | None = None,
        issuer: str | None = None,
        audience: str | None = None,
        access_minutes: int | None = None,
        refresh_days: int | None = None,
    ):
        self.secret = settings.auth_jwt_secret if secret is None else secret
        self.algorithm = algorithm or settings.auth_jwt_algorithm
        self.issuer = issuer or settings.auth_issuer
        self.audience = audience or settings.auth_audience
        self.access_minutes = (
            access_minutes
            if access_minutes is not None
            else settings.auth_access_token_expire_minutes
        )
        self.refresh_days = (
            refresh_days
            if refresh_days is not None
            else settings.auth_refresh_token_expire_days
        )

    def _ensure_configured(self) -> None:
        if is_unsafe_auth_jwt_secret(self.secret):
            raise ServiceUnavailableError(
                "认证服务密钥未配置或强度不足",
                error_code="AUTH_NOT_CONFIGURED",
            )
        if self.algorithm != "HS256":
            raise ServiceUnavailableError(
                "当前版本只允许HS256认证算法",
                error_code="AUTH_ALGORITHM_UNSUPPORTED",
            )

    def _encode(
        self,
        *,
        user_id: str,
        token_type: TokenType,
        expires_at: datetime,
        now: datetime,
        jti: str,
    ) -> str:
        self._ensure_configured()
        payload: dict[str, Any] = {
            "sub": user_id,
            "jti": jti,
            "type": token_type.value,
            "iat": now,
            "exp": expires_at,
            "iss": self.issuer,
            "aud": self.audience,
        }
        return jwt.encode(payload, self.secret, algorithm=self.algorithm)

    def create_pair(
        self,
        user_id: str,
        *,
        now: datetime | None = None,
    ) -> TokenPair:
        issued_at = now or utc_now()
        access_expires = issued_at + timedelta(
            minutes=self.access_minutes
        )
        refresh_expires = issued_at + timedelta(days=self.refresh_days)
        refresh_jti = uuid4().hex
        return TokenPair(
            access_token=self._encode(
                user_id=user_id,
                token_type=TokenType.ACCESS,
                expires_at=access_expires,
                now=issued_at,
                jti=uuid4().hex,
            ),
            refresh_token=self._encode(
                user_id=user_id,
                token_type=TokenType.REFRESH,
                expires_at=refresh_expires,
                now=issued_at,
                jti=refresh_jti,
            ),
            access_expires_in=self.access_minutes * 60,
            refresh_jti=refresh_jti,
            refresh_expires_at=refresh_expires,
        )

    def decode(
        self,
        token: str,
        *,
        expected_type: TokenType,
    ) -> TokenClaims:
        self._ensure_configured()
        try:
            payload = jwt.decode(
                token,
                self.secret,
                algorithms=[self.algorithm],
                issuer=self.issuer,
                audience=self.audience,
                options={
                    "require": [
                        "sub",
                        "jti",
                        "type",
                        "iat",
                        "exp",
                        "iss",
                        "aud",
                    ]
                },
            )
            token_type = TokenType(payload["type"])
            if token_type != expected_type:
                raise ValueError("token type mismatch")
            user_id = str(payload["sub"]).strip()
            jti = str(payload["jti"]).strip()
            if not user_id or not jti:
                raise ValueError("empty identity")
            return TokenClaims(
                user_id=user_id,
                jti=jti,
                token_type=token_type,
                issued_at=datetime.fromtimestamp(
                    int(payload["iat"]), tz=utc_now().tzinfo
                ),
                expires_at=datetime.fromtimestamp(
                    int(payload["exp"]), tz=utc_now().tzinfo
                ),
            )
        except (jwt.InvalidTokenError, KeyError, TypeError, ValueError) as exc:
            raise AuthenticationError(
                "身份凭证无效或已过期",
                error_code="INVALID_TOKEN",
            ) from exc

    @staticmethod
    def hash_refresh_token(token: str) -> str:
        """数据库只保存Refresh Token的SHA-256摘要。"""

        return hashlib.sha256(token.encode("utf-8")).hexdigest()
