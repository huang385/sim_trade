"""Runs independent derivative and cash-security tick coordinators."""

from app.schemas.matching_schema import MarketTickMatchResult


class MarketTickMatchingRouter:
    cash_security_instrument_types = frozenset({"STOCK", "CONVERTIBLE_BOND"})

    def __init__(self, *, derivative_service, cash_security_service) -> None:
        self.derivative_service = derivative_service
        self.cash_security_service = cash_security_service

    @property
    def matching_engine(self):
        """Compatibility view for worker diagnostics and existing callers."""
        return self.derivative_service.matching_engine

    def process(self, *, stream_message_id: str, fields) -> MarketTickMatchResult:
        # 每条 Tick 只读取一次 Redis 候选订单集合，并批量查询一次 PostgreSQL。
        # 在任一执行器看到订单前先按产品分流，避免双方反复探测、跳过对方订单。
        event = self.derivative_service.parse_event(fields)
        order_ids = sorted(
            self.derivative_service.active_order_index.list_instrument_order_ids(
                event.exchange_id,
                event.symbol,
            )
        )
        if not order_ids:
            return MarketTickMatchResult(
                candidate_count=0,
                matched_count=0,
                settled_count=0,
                idempotent_count=0,
                skipped_count=0,
            )

        with self.derivative_service.session_factory() as db:
            orders = self.derivative_service.order_repository.list_by_order_ids(
                db,
                order_ids,
            )
            orders_by_id = {order.order_id: order for order in orders}
            cash_ids = [
                order_id
                for order_id in order_ids
                if (
                    (order := orders_by_id.get(order_id)) is not None
                    and order.instrument_type in self.cash_security_instrument_types
                )
            ]
            cash_id_set = set(cash_ids)
            derivative_ids = [
                order_id for order_id in order_ids if order_id not in cash_id_set
            ]
            empty_result = MarketTickMatchResult(0, 0, 0, 0, 0)
            derivative = (
                self.derivative_service.process_routed_orders(
                    order_ids=derivative_ids,
                    event=event,
                    stream_message_id=stream_message_id,
                    orders_by_id=orders_by_id,
                )
                if derivative_ids
                else empty_result
            )
            cash = (
                self.cash_security_service.process_routed_orders(
                    order_ids=cash_ids,
                    event=event,
                    stream_message_id=stream_message_id,
                    orders_by_id=orders_by_id,
                )
                if cash_ids
                else empty_result
            )
        return MarketTickMatchResult(
            candidate_count=derivative.candidate_count + cash.candidate_count,
            matched_count=derivative.matched_count + cash.matched_count,
            settled_count=derivative.settled_count + cash.settled_count,
            idempotent_count=derivative.idempotent_count + cash.idempotent_count,
            skipped_count=derivative.skipped_count + cash.skipped_count,
        )
