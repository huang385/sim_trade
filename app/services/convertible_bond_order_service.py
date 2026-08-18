"""Convertible-bond order entry at the cash-security boundary."""

from app.services.stock_order_cancellation_service import CashSecurityOrderCancellationService
from app.services.stock_order_service import CashSecurityOrderService
from app.services.stock_order_validation_service import ConvertibleBondTradingPolicy


class ConvertibleBondOrderService(CashSecurityOrderService):
    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("instrument_type", "CONVERTIBLE_BOND")
        kwargs.setdefault("accepted_event_type", "CONVERTIBLE_BOND_ORDER_ACCEPTED")
        kwargs.setdefault("validation_service", ConvertibleBondTradingPolicy())
        super().__init__(**kwargs)


class ConvertibleBondOrderCancellationService(CashSecurityOrderCancellationService):
    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("instrument_type", "CONVERTIBLE_BOND")
        kwargs.setdefault("cancelled_event_type", "CONVERTIBLE_BOND_ORDER_CANCELLED")
        super().__init__(**kwargs)


_order_service = ConvertibleBondOrderService()
_cancellation_service = ConvertibleBondOrderCancellationService()


def get_convertible_bond_order_service() -> ConvertibleBondOrderService:
    return _order_service


def get_convertible_bond_order_cancellation_service() -> ConvertibleBondOrderCancellationService:
    return _cancellation_service
