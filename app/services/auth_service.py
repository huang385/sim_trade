from dataclasses import dataclass
from datetime import timedelta
import hmac

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.common.exceptions import (
    AuthenticationError,
    DataAccessError,
)
from app.common.time_utils import utc_now
from app.core.config import settings
from app.enums.auth_enums import TokenType, UserStatus
from app.models.app_user import AppUser
from app.models.auth_refresh_session import AuthRefreshSession
from app.repositories.auth_refresh_session_repository import (
    AuthRefreshSessionRepository,
)
from app.repositories.user_repository import UserRepository
from app.services.login_rate_limit_service import LoginRateLimitService
from app.services.password_service import PasswordService
from app.services.token_service import TokenPair, TokenService


@dataclass(frozen=True)
class AuthResult:
    """认证流程提交成功后返回给API层的用户和Token。"""

    user: AppUser
    tokens: TokenPair


class AuthService:
    """编排登录、Refresh轮换和Logout事务。"""

    def __init__(
        self,
        *,
        user_repository: UserRepository,
        refresh_repository: AuthRefreshSessionRepository,
        password_service: PasswordService,
        token_service: TokenService,
        rate_limit_service: LoginRateLimitService,
    ):
        self.user_repository = user_repository
        self.refresh_repository = refresh_repository
        self.password_service = password_service
        self.token_service = token_service
        self.rate_limit_service = rate_limit_service

    @staticmethod
    def _invalid_credentials() -> AuthenticationError:
        # 用户不存在、密码错误、禁用和锁定统一返回，避免用户名枚举。
        return AuthenticationError(
            "用户名或密码错误",
            error_code="INVALID_CREDENTIALS",
        )

    def _new_refresh_session(
        self,
        *,
        result: TokenPair,
        user_id: str,
        user_agent: str | None,
        client_ip: str | None,
    ) -> AuthRefreshSession:
        return AuthRefreshSession(
            jti=result.refresh_jti,
            user_id=user_id,
            token_hash=self.token_service.hash_refresh_token(
                result.refresh_token
            ),
            expires_at=result.refresh_expires_at,
            user_agent=(user_agent or "")[:512] or None,
            client_ip=(client_ip or "")[:128] or None,
        )

    def login(
        self,
        db: Session,
        *,
        username: str,
        password: str,
        client_ip: str,
        user_agent: str | None,
    ) -> AuthResult:
        """验证登录，并在同一事务创建可撤销Refresh会话。"""

        self.rate_limit_service.check(client_ip)
        normalized = username.strip().lower()
        user = self.user_repository.get_by_username_for_update(
            db, normalized
        )
        if user is None:
            self.password_service.verify_dummy(password)
            db.rollback()
            raise self._invalid_credentials()

        now = utc_now()
        if (
            user.status == UserStatus.LOCKED.value
            and user.locked_until is not None
            and user.locked_until <= now
        ):
            user.status = UserStatus.ACTIVE.value
            user.failed_login_count = 0
            user.locked_until = None

        if user.status != UserStatus.ACTIVE.value:
            db.rollback()
            raise self._invalid_credentials()

        if not self.password_service.verify_password(
            user.password_hash, password
        ):
            user.failed_login_count += 1
            if (
                user.failed_login_count
                >= settings.auth_max_login_failures
            ):
                user.status = UserStatus.LOCKED.value
                user.locked_until = now + timedelta(
                    minutes=settings.auth_login_lock_minutes
                )
            try:
                db.commit()
            except SQLAlchemyError as exc:
                db.rollback()
                raise DataAccessError("更新登录失败状态失败") from exc
            raise self._invalid_credentials()

        if self.password_service.needs_rehash(user.password_hash):
            user.password_hash = self.password_service.hash_password(
                password
            )
            user.password_changed_at = now
        user.failed_login_count = 0
        user.locked_until = None
        user.last_login_at = now

        tokens = self.token_service.create_pair(user.user_id, now=now)
        self.refresh_repository.add(
            db,
            self._new_refresh_session(
                result=tokens,
                user_id=user.user_id,
                user_agent=user_agent,
                client_ip=client_ip,
            ),
        )
        try:
            db.commit()
            db.refresh(user)
            return AuthResult(user=user, tokens=tokens)
        except SQLAlchemyError as exc:
            db.rollback()
            raise DataAccessError("创建登录会话失败") from exc

    def refresh(
        self,
        db: Session,
        *,
        refresh_token: str,
        client_ip: str,
        user_agent: str | None,
    ) -> AuthResult:
        """按AppUser→RefreshSession顺序锁定并原子轮换Refresh Token。"""

        claims = self.token_service.decode(
            refresh_token,
            expected_type=TokenType.REFRESH,
        )
        # 认证事务统一先锁用户，再锁Refresh会话。改密和禁用也采用该顺序，
        # 避免两个事务分别持有一端行锁并相互等待形成PostgreSQL死锁。
        user = self.user_repository.get_by_user_id_for_update(
            db, claims.user_id
        )
        if (
            user is None
            or user.status != UserStatus.ACTIVE.value
            or user.user_id != claims.user_id
        ):
            db.rollback()
            raise AuthenticationError(
                "Refresh Token已失效",
                error_code="REFRESH_TOKEN_INVALID",
            )

        refresh_session = (
            self.refresh_repository.get_by_jti_for_update(db, claims.jti)
        )
        now = utc_now()
        expected_hash = self.token_service.hash_refresh_token(refresh_token)
        if (
            refresh_session is None
            or refresh_session.revoked_at is not None
            or refresh_session.expires_at <= now
            or not hmac.compare_digest(
                refresh_session.token_hash, expected_hash
            )
            or claims.user_id != refresh_session.user_id
            or user.user_id != refresh_session.user_id
        ):
            db.rollback()
            raise AuthenticationError(
                "Refresh Token已失效",
                error_code="REFRESH_TOKEN_INVALID",
            )

        tokens = self.token_service.create_pair(user.user_id, now=now)
        refresh_session.revoked_at = now
        refresh_session.last_used_at = now
        refresh_session.replaced_by_jti = tokens.refresh_jti
        self.refresh_repository.add(
            db,
            self._new_refresh_session(
                result=tokens,
                user_id=user.user_id,
                user_agent=user_agent,
                client_ip=client_ip,
            ),
        )
        try:
            db.commit()
            db.refresh(user)
            return AuthResult(user=user, tokens=tokens)
        except (IntegrityError, SQLAlchemyError) as exc:
            db.rollback()
            raise DataAccessError("轮换Refresh Token失败") from exc

    def logout(
        self,
        db: Session,
        *,
        refresh_token: str | None,
    ) -> None:
        """撤销当前Refresh会话；缺失、过期或重复Logout均保持幂等。"""

        if not refresh_token:
            db.rollback()
            return
        try:
            claims = self.token_service.decode(
                refresh_token,
                expected_type=TokenType.REFRESH,
            )
        except AuthenticationError:
            db.rollback()
            return
        refresh_session = (
            self.refresh_repository.get_by_jti_for_update(db, claims.jti)
        )
        if refresh_session is None:
            db.rollback()
            return
        if (
            refresh_session.revoked_at is None
            and hmac.compare_digest(
                refresh_session.token_hash,
                self.token_service.hash_refresh_token(refresh_token),
            )
        ):
            refresh_session.revoked_at = utc_now()
            refresh_session.last_used_at = refresh_session.revoked_at
        try:
            db.commit()
        except SQLAlchemyError as exc:
            db.rollback()
            raise DataAccessError("退出登录失败") from exc
