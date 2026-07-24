from app.enums.order_enums import OrderDirection
from app.matching.models import MatchResult, MatchingMarketData, MatchingOrder


class VnMatchingEngine:
    """
    VN 模式的一档限价纯撮合引擎。

    VN 模式不是交易所共享订单簿：每个候选订单独立看到完整的一档盘口量，
    不同订单之间不会共同扣减 ask_volume_1 或 bid_volume_1。
    """

    name = "VN"
    version = "1.0"

    def _not_matched(
        self,
        order: MatchingOrder,
        market: MatchingMarketData,
        reason: str,
    ) -> MatchResult:
        """
        构造统一的不成交结果。

        价格未触达和盘口无可用量属于正常计算结果，不能通过抛异常表达，
        否则 Worker 会把正常未成交误判为需要重试的基础设施故障。
        """

        return MatchResult(
            matched=False,
            order_id=order.order_id,
            market_event_id=market.event_id,
            market_stream_message_id=market.stream_message_id,
            fill_price=None,
            fill_volume=0,
            tick_event_time=market.event_time,
            tick_sequence_id=market.sequence_id,
            reason=reason,
            engine_name=self.name,
            engine_version=self.version,
        )

    def match(
        self,
        order: MatchingOrder,
        market: MatchingMarketData,
    ) -> MatchResult:
        """
        根据买卖方向使用对手一价计算成交价格与数量。

        LIMIT/OPEN 是否为当前业务支持范围由 MarketTickMatchingService
        提前过滤。核心算法只处理方向、限价、剩余量和盘口量，因此未来
        CLOSE 类订单可以复用同一套价格撮合逻辑。
        """

        # Redis 活动索引可能短暂滞后；无剩余数量时直接返回不成交。
        if order.remaining_volume <= 0:
            return self._not_matched(order, market, "NO_REMAINING_VOLUME")

        if order.direction == OrderDirection.BUY:
            # 买入委托与卖一比较，满足时按卖一价成交。
            fill_price = market.ask_price_1
            market_volume = market.ask_volume_1
            if fill_price is None or fill_price <= 0:
                return self._not_matched(order, market, "INVALID_ASK_PRICE")
            if market_volume <= 0:
                return self._not_matched(order, market, "NO_ASK_VOLUME")
            if order.limit_price < fill_price:
                return self._not_matched(order, market, "BUY_LIMIT_NOT_REACHED")
        elif order.direction == OrderDirection.SELL:
            # 卖出委托与买一比较，满足时按买一价成交。
            fill_price = market.bid_price_1
            market_volume = market.bid_volume_1
            if fill_price is None or fill_price <= 0:
                return self._not_matched(order, market, "INVALID_BID_PRICE")
            if market_volume <= 0:
                return self._not_matched(order, market, "NO_BID_VOLUME")
            if order.limit_price > fill_price:
                return self._not_matched(order, market, "SELL_LIMIT_NOT_REACHED")
        else:
            # 防御非法快照；正常订单在进入本模块前已经完成方向校验。
            return self._not_matched(order, market, "UNSUPPORTED_DIRECTION")

        # 两个输入数量均已确认大于零，min 结果必定处于
        # 1..order.remaining_volume 范围内，支持盘口不足时部分成交。
        fill_volume = min(order.remaining_volume, market_volume)
        return MatchResult(
            matched=True,
            order_id=order.order_id,
            market_event_id=market.event_id,
            market_stream_message_id=market.stream_message_id,
            fill_price=fill_price,
            fill_volume=fill_volume,
            tick_event_time=market.event_time,
            tick_sequence_id=market.sequence_id,
            reason=None,
            engine_name=self.name,
            engine_version=self.version,
        )
