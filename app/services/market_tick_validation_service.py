from app.schemas.market_tick_schema import MarketTick


class MarketTickValidationError(ValueError):
    """可预期的坏行情，不应导致订阅Worker退出。"""


class MarketTickValidationService:
    """
    校验消息结构及主程序必须掌握的合约状态。

    数值正负、买卖一交叉和交易所一致性由上游行情源负责，本层不重复校验。
    """

    @staticmethod
    def validate_envelope(*, data: dict, raw: dict) -> None:
        del raw
        if data.get("action") != "feed":
            raise MarketTickValidationError("行情action不是feed")
        channel = str(data.get("channel") or "").strip()
        if not channel.startswith("tick_"):
            raise MarketTickValidationError("行情频道不是tick")
        if not str(data.get("order_book_id") or "").strip():
            raise MarketTickValidationError("行情order_book_id不能为空")

    @classmethod
    def validate(cls, *, tick: MarketTick, instrument) -> None:
        if instrument is None:
            raise MarketTickValidationError("合约不存在")
        if not instrument.is_active:
            raise MarketTickValidationError("合约不可交易")
        if tick.order_book_id != instrument.order_book_id:
            raise MarketTickValidationError("行情合约与参考数据不一致")

        # local_recv_time可能受机器时钟偏差影响，不能与event_time比较后拒绝行情。
        # trading_day只使用行情源明确提供的字段，绝不从event_time推算。
