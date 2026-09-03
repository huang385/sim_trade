"""ETF secondary-market order entry at the cash-security boundary."""

from app.services.stock_order_cancellation_service import (
    CashSecurityOrderCancellationService,
)
from app.services.stock_order_service import CashSecurityOrderService
from app.services.stock_order_validation_service import EtfTradingPolicy


class EtfOrderService(CashSecurityOrderService):
    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("instrument_type", "ETF")
        kwargs.setdefault("accepted_event_type", "ETF_ORDER_ACCEPTED")
        kwargs.setdefault("entry_enabled_setting", "etf_order_entry_enabled")
        kwargs.setdefault("validation_service", EtfTradingPolicy())
        super().__init__(**kwargs)


class EtfOrderCancellationService(CashSecurityOrderCancellationService):
    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("instrument_type", "ETF")
        kwargs.setdefault("cancelled_event_type", "ETF_ORDER_CANCELLED")
        kwargs.setdefault("entry_enabled_setting", "etf_order_entry_enabled")
        super().__init__(**kwargs)


_order_service = EtfOrderService()
_cancellation_service = EtfOrderCancellationService()


def get_etf_order_service() -> EtfOrderService:
    return _order_service


def get_etf_order_cancellation_service() -> EtfOrderCancellationService:
    return _cancellation_service
