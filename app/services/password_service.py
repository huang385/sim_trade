import secrets
import threading

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error

from app.common.exceptions import RateLimitError
from app.core.config import settings


class PasswordService:
    """使用argon2-cffi提供的Argon2id实现密码哈希和验证。"""

    def __init__(
        self,
        hasher: PasswordHasher | None = None,
        *,
        max_verify_concurrency: int | None = None,
        verify_acquire_timeout_seconds: float | None = None,
    ):
        self.hasher = hasher or PasswordHasher()
        concurrency = (
            settings.auth_password_verify_max_concurrency
            if max_verify_concurrency is None
            else max_verify_concurrency
        )
        self._verify_slots = threading.BoundedSemaphore(
            max(1, int(concurrency))
        )
        self._verify_acquire_timeout_seconds = max(
            0.0,
            float(
                settings.auth_password_verify_acquire_timeout_seconds
                if verify_acquire_timeout_seconds is None
                else verify_acquire_timeout_seconds
            ),
        )
        # 未命中用户时仍执行一次同成本验证，降低用户名枚举的时间差异。
        self._dummy_hash = self.hasher.hash(secrets.token_urlsafe(32))

    def hash_password(self, password: str) -> str:
        """生成包含随机盐和参数的不可逆Argon2id哈希。"""

        return self.hasher.hash(password)

    def verify_password(self, password_hash: str, password: str) -> bool:
        """无论密码不匹配还是哈希格式损坏，都只返回验证失败。"""

        acquired = self._verify_slots.acquire(
            timeout=self._verify_acquire_timeout_seconds
        )
        if not acquired:
            raise RateLimitError(
                "登录验证繁忙，请稍后重试",
                error_code="PASSWORD_VERIFY_BUSY",
            )
        try:
            return bool(self.hasher.verify(password_hash, password))
        except Argon2Error:
            return False
        finally:
            self._verify_slots.release()

    def verify_dummy(self, password: str) -> None:
        """用户不存在时执行固定假哈希验证，不泄露用户名是否存在。"""

        self.verify_password(self._dummy_hash, password)

    def needs_rehash(self, password_hash: str) -> bool:
        try:
            return self.hasher.check_needs_rehash(password_hash)
        except Argon2Error:
            return True
