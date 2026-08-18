"""Runs independent derivative and cash-security tick coordinators."""

from app.schemas.matching_schema import MarketTickMatchResult


class MarketTickMatchingRouter:
    def __init__(self, *, derivative_service, cash_security_service) -> None:
        self.derivative_service = derivative_service
        self.cash_security_service = cash_security_service

    @property
    def matching_engine(self):
        """Compatibility view for worker diagnostics and existing callers."""
        return self.derivative_service.matching_engine

    def process(self, *, stream_message_id: str, fields) -> MarketTickMatchResult:
        derivative = self.derivative_service.process(
            stream_message_id=stream_message_id, fields=fields
        )
        cash = self.cash_security_service.process(
            stream_message_id=stream_message_id, fields=fields
        )
        return MarketTickMatchResult(
            candidate_count=derivative.candidate_count + cash.candidate_count,
            matched_count=derivative.matched_count + cash.matched_count,
            settled_count=derivative.settled_count + cash.settled_count,
            idempotent_count=derivative.idempotent_count + cash.idempotent_count,
            skipped_count=derivative.skipped_count + cash.skipped_count,
        )
