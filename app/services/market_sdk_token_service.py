from sqlalchemy.orm import Session

from app.models.market_sdk_token_binding import MarketSdkTokenBinding


class MarketSdkTokenService:
    """按客户端来源IP查询绑定的行情SDK凭证，供登录响应发放。"""

    @staticmethod
    def find_binding(
        db: Session, client_ip: str | None
    ) -> MarketSdkTokenBinding | None:
        if not client_ip or client_ip == "unknown":
            return None
        return (
            db.query(MarketSdkTokenBinding)
            .filter(MarketSdkTokenBinding.client_ip == client_ip)
            .first()
        )

    @staticmethod
    def grant_payload(
        binding: MarketSdkTokenBinding | None,
    ) -> dict | None:
        """把绑定行转成登录响应的market_sdk字段；无绑定时返回None。"""

        if binding is None:
            return None
        return {
            "live_token": binding.live_sdk_token,
            "data_token": binding.data_sdk_token,
            "mode": binding.mode,
            "live_server_url": binding.live_server_url or "",
            "data_server_url": binding.data_server_url or "",
        }
