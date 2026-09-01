from datetime import datetime

from sqlalchemy import DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.common.time_utils import utc_now
from app.core.database import Base


class MarketSdkTokenBinding(Base):
    """按客户端来源IP绑定的行情SDK凭证；登录成功后随响应发放给终端。

    局域网终端直连行情中心使用：实时行情SDK（ymm-live-data-sdk）和
    数据库行情SDK（ymm-data-sdk）分别持有独立token，行情中心本身也
    按来源IP授权，因此这里只做IP→token的静态绑定查询，不做凭证加密。
    """

    __tablename__ = "market_sdk_token_binding"
    __table_args__ = (
        UniqueConstraint("client_ip", name="uq_market_sdk_token_binding_client_ip"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    client_ip: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    live_sdk_token: Mapped[str] = mapped_column(Text, nullable=False)
    data_sdk_token: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="lan"
    )
    live_server_url: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )
    data_server_url: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )
    remark: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
