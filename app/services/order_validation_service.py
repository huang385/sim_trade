from decimal import Decimal

from app.common.decimal_utils import quantize_money
from app.common.exceptions import (
    BusinessRuleError,
    BusinessValidationError,
    ResourceConflictError,
)
from app.enums.order_enums import OffsetFlag, OrderType
from app.models.instrument import Instrument
from app.models.order import Order
from app.schemas.order_schema import OrderCreateRequest, StockOrderCreateRequest


class OrderValidationService:
    """
    与账户资金无关的订单基础校验服务。

    本服务只判断订单和合约本身是否合法，不查询账户余额，
    也不修改任何数据库对象。

    校验顺序：
    1. 合约存在且处于可交易状态；
    2. 当前版本只接受期货限价开仓和平仓；
    3. 价格和数量必须为正数；
    4. 数量符合合约最小、最大下单量；
    5. 价格符合合约最小变动价位。
    """

    @classmethod
    def validate_order(
        cls,
        *,
        request: OrderCreateRequest,
        instrument: Instrument | None,
        resolved_price: Decimal | None = None,
    ) -> None:
        """校验一笔限价开仓或平仓订单的公共规则。"""

        # RuleQueryService 正常情况下已经检查过合约，
        # 这里仍保留防御性校验，便于该服务被单独调用。
        if instrument is None:
            raise BusinessRuleError(
                "合约不存在",
                error_code="INSTRUMENT_NOT_FOUND",
            )

        if not instrument.is_active:
            raise BusinessRuleError(
                "合约当前不可交易",
                error_code="INSTRUMENT_INACTIVE",
            )

        if request.order_type not in set(OrderType):
            raise BusinessValidationError(
                "不支持的订单价格类型",
                error_code="UNSUPPORTED_ORDER_TYPE",
            )

        # 第一阶段不接收市价单、条件单等其他订单类型。
        if request.offset_flag not in {
            OffsetFlag.OPEN,
            OffsetFlag.CLOSE,
            OffsetFlag.CLOSE_TODAY,
            OffsetFlag.CLOSE_YESTERDAY,
        }:
            raise BusinessValidationError(
                "不支持的开平标志",
                error_code="UNSUPPORTED_OFFSET_FLAG",
            )

        price = resolved_price if resolved_price is not None else request.limit_price
        if price is None or price <= Decimal("0"):
            raise BusinessValidationError(
                "委托价格必须大于0",
                error_code="INVALID_ORDER_PRICE",
            )

        if request.volume <= 0:
            raise BusinessValidationError(
                "委托数量必须大于0",
                error_code="INVALID_ORDER_VOLUME",
            )

        # 数量上下限来自合约参考数据，不能在订单接口中写死。
        if request.volume < instrument.min_volume:
            raise BusinessValidationError(
                f"委托数量不能小于 {instrument.min_volume}",
                error_code="VOLUME_BELOW_MINIMUM",
            )

        if request.volume > instrument.max_volume:
            raise BusinessValidationError(
                f"委托数量不能大于 {instrument.max_volume}",
                error_code="VOLUME_ABOVE_MAXIMUM",
            )

        # 价格档位必须使用 Decimal 计算，禁止转换成 float。
        cls.validate_price_tick(
            price=price,
            price_tick=instrument.price_tick,
        )

    @classmethod
    def validate_open_order(
        cls,
        *,
        request: OrderCreateRequest,
        instrument: Instrument | None,
    ) -> None:
        """兼容原有调用：校验公共规则后明确要求OPEN。"""

        cls.validate_order(request=request, instrument=instrument)
        if request.offset_flag != OffsetFlag.OPEN:
            raise BusinessValidationError(
                "当前方法只校验开仓订单",
                error_code="UNSUPPORTED_OFFSET_FLAG",
            )

    @staticmethod
    def validate_idempotent_order_request(
        *,
        existing_order: Order,
        request: OrderCreateRequest | StockOrderCreateRequest,
    ) -> None:
        """
        校验同一client_order_id是否仍代表同一笔业务请求。

        只比较不会随撮合和撤单变化的原始下单字段；价格统一按订单表六位
        Decimal精度比较，禁止经过float。任一字段变化都拒绝复用幂等键。
        """

        request_instrument_type = (
            "STOCK" if isinstance(request, StockOrderCreateRequest) else None
        )
        existing_instrument_type = getattr(
            existing_order, "instrument_type", "FUTURES"
        )
        if (
            request_instrument_type is not None
            and existing_instrument_type != request_instrument_type
        ) or (
            request_instrument_type is None
            and existing_instrument_type == "STOCK"
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
            (
                quantize_money(request.limit_price)
                if request.limit_price is not None
                else None
            ),
            request.volume,
        )
        if existing_fields != request_fields:
            raise ResourceConflictError(
                "client_order_id已被其他订单请求使用",
                error_code="IDEMPOTENCY_KEY_REUSED",
            )

    @staticmethod
    def validate_price_tick(
        *,
        price: Decimal,
        price_tick: Decimal,
    ) -> None:
        """
        使用 Decimal 校验最小变动价位。

        示例：
        price_tick=1 时，3500 和 3501 合法，3500.5 非法。
        Decimal 取模不会产生二进制浮点数的精度误差。
        """

        if price_tick <= Decimal("0"):
            raise BusinessValidationError(
                "最小变动价位不合法",
                error_code="INVALID_PRICE_TICK",
            )

        # 能被 price_tick 整除，表示价格正好落在合法档位上。
        if price % price_tick != Decimal("0"):
            raise BusinessValidationError(
                f"委托价格不符合最小变动价位 {price_tick}",
                error_code="PRICE_TICK_MISMATCH",
            )
