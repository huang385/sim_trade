from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.common.time_utils import utc_now
from app.core.database import Base
from app.enums.auth_enums import UserRole, UserStatus


class AppUser(Base):
    """平台登录用户；与用于交易结算的Account保持明确分离。"""

    __tablename__ = "app_user"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_app_user_user_id"),
        UniqueConstraint("username", name="uq_app_user_username"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    username: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(
        String(32), nullable=False, default=UserRole.USER.value
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=UserStatus.ACTIVE.value
    )
    failed_login_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    password_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
