from pydantic import BaseModel


class WebSocketTicketResponse(BaseModel):
    """经过认证的用户取得的一次性WebSocket连接票据。"""

    ticket: str
    expires_in: int
