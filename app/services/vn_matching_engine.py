from app.enums.order_enums import OffsetFlag, OrderDirection, OrderType
from app.schemas.market_tick_schema import MarketTick
from app.schemas.matching_schema import MatchableOrder, MatchResult


class VNMatchingEngine:
    """VN 模式的限价开仓纯撮合引擎。

    VN 模式不是交易所订单簿：每个候选订单都独立看到完整的一档盘口量，
    不同订单之间不会共同扣减 ask_volume_1 或 bid_volume_1。
    """

    @staticmethod
    def _not_matched(
        order: MatchableOrder,
        tick: MarketTick,
        market_stream_message_id: str,
        reason: str,
    ) -> MatchResult:
        """
        构造统一的不成交结果。

        不成交不是系统异常，Worker可以正常ACK该Tick，因此必须用明确reason
        与数据库失败区分，不能通过抛异常表示正常的价格未触达。
        """

        return MatchResult(
            matched=False,
            order_id=order.order_id,
            market_event_id=tick.source_event_id,
            market_stream_message_id=market_stream_message_id,
            fill_price=None,
            fill_volume=0,
            tick_event_time=tick.event_time,
            tick_sequence_id=tick.sequence_id,
            reason=reason,
        )

    def match_limit_open_order(
        self,
        *,
        order: MatchableOrder,
        tick: MarketTick,
        market_stream_message_id: str,
    ) -> MatchResult:
        """
        按买卖方向使用对手一价判断限价开仓订单是否成交。

        本方法只读取不可变快照并返回MatchResult，不修改传入对象。价格比较
        全程使用Decimal，避免期货小数价格经过float后出现边界误判。
        """

        # 当前阶段明确拒绝市价单、条件单等尚未实现的订单类型。
        if order.order_type != OrderType.LIMIT.value:
            return self._not_matched(
                order, tick, market_stream_message_id, "UNSUPPORTED_ORDER_TYPE"
            )
        # 平仓需要持仓冻结和今昨仓选择，不能复用当前开仓撮合链路。
        if order.offset_flag != OffsetFlag.OPEN.value:
            return self._not_matched(
                order, tick, market_stream_message_id, "UNSUPPORTED_OFFSET_FLAG"
            )
        # Redis索引可能短暂滞后；没有剩余量的候选订单直接视为不成交。
        if order.remaining_volume <= 0:
            return self._not_matched(
                order, tick, market_stream_message_id, "NO_REMAINING_VOLUME"
            )

        if order.direction == OrderDirection.BUY.value:
            # 买入委托主动与卖一价比较，并使用卖一数量作为单笔成交上限。
            price = tick.ask_price_1
            volume = tick.ask_volume_1
            if price is None or price <= 0:
                return self._not_matched(
                    order, tick, market_stream_message_id, "INVALID_ASK_PRICE"
                )
            if volume <= 0:
                return self._not_matched(
                    order, tick, market_stream_message_id, "NO_ASK_VOLUME"
                )
            if order.limit_price < price:
                return self._not_matched(
                    order, tick, market_stream_message_id, "BUY_LIMIT_NOT_REACHED"
                )
        elif order.direction == OrderDirection.SELL.value:
            # 卖出委托主动与买一价比较，并使用买一数量作为单笔成交上限。
            price = tick.bid_price_1
            volume = tick.bid_volume_1
            if price is None or price <= 0:
                return self._not_matched(
                    order, tick, market_stream_message_id, "INVALID_BID_PRICE"
                )
            if volume <= 0:
                return self._not_matched(
                    order, tick, market_stream_message_id, "NO_BID_VOLUME"
                )
            if order.limit_price > price:
                return self._not_matched(
                    order, tick, market_stream_message_id, "SELL_LIMIT_NOT_REACHED"
                )
        else:
            # 防御数据库或缓存中出现未知方向；正常下单校验不会走到这里。
            return self._not_matched(
                order, tick, market_stream_message_id, "UNSUPPORTED_DIRECTION"
            )

        # VN独立流动性规则：这里只限制当前一笔订单，不扣减共享盘口量。
        # 下一笔候选订单仍然能够独立使用完整的买一或卖一数量。
        return MatchResult(
            matched=True,
            order_id=order.order_id,
            market_event_id=tick.source_event_id,
            market_stream_message_id=market_stream_message_id,
            fill_price=price,
            fill_volume=min(order.remaining_volume, volume),
            tick_event_time=tick.event_time,
            tick_sequence_id=tick.sequence_id,
            reason=None,
        )
