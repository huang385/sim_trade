"""产品无关的订单幂等请求比较。"""

from decimal import Decimal

from app.common.decimal_utils import quantize_money
from app.common.exceptions import ResourceConflictError
from app.enums.order_enums import OrderType
from app.models.order import Order
from app.schemas.order_schema import OrderCreateRequest, StockOrderCreateRequest


class OrderIdempotencyService:
    @staticmethod
    def validate(
        *,
        existing_order: Order,
        request: OrderCreateRequest | StockOrderCreateRequest,
    ) -> None:
        # 可转债请求继承股票请求结构，不能依赖 isinstance 判断产品；必须读取
        # 服务端已加载的合约类型，避免相同请求形状被错误分派。
        request_instrument_type = getattr(
            request, "cash_security_instrument_type", None
        )
        existing_instrument_type = getattr(
            existing_order, "instrument_type", "FUTURES"
        )
        if (
            request_instrument_type is not None
            and existing_instrument_type != request_instrument_type
        ) or (
            request_instrument_type is None
            and existing_instrument_type in {"STOCK", "CONVERTIBLE_BOND"}
        ):
            raise ResourceConflictError(
                "client_order_id 已被不同产品类型的订单使用",
                error_code="IDEMPOTENCY_KEY_REUSED",
            )
        request_offset_flag = getattr(request, "offset_flag", None)
        existing_fields = (
            existing_instrument_type,
            existing_order.account_id.strip().upper(),
            existing_order.exchange_id.strip().upper(),
            existing_order.symbol.strip().upper(),
            existing_order.direction,
            existing_order.offset_flag,
            existing_order.order_type,
            (
                quantize_money(Decimal(existing_order.submitted_limit_price))
                if getattr(existing_order, "submitted_limit_price", None) is not None
                else quantize_money(Decimal(existing_order.limit_price))
                if existing_order.order_type == OrderType.LIMIT.value
                else None
            ),
            existing_order.total_volume,
        )
        request_fields = (
            request_instrument_type or existing_instrument_type,
            request.account_id.strip().upper(),
            request.exchange_id.strip().upper(),
            request.symbol.strip().upper(),
            getattr(request.direction, "value", request.direction),
            getattr(request_offset_flag, "value", request_offset_flag),
            getattr(request.order_type, "value", request.order_type),
            quantize_money(request.limit_price) if request.limit_price is not None else None,
            request.volume,
        )
        if existing_fields != request_fields:
            raise ResourceConflictError(
                "client_order_id已被其他订单请求使用",
                error_code="IDEMPOTENCY_KEY_REUSED",
            )
