from decimal import Decimal

from app.common.exceptions import (
    BusinessRuleError,
    BusinessValidationError,
)
from app.enums.order_enums import OffsetFlag, OrderType
from app.models.instrument import Instrument
from app.schemas.order_schema import OrderCreateRequest


class OrderValidationService:
    """
    与账户资金无关的订单基础校验服务。

    本服务只判断订单和合约本身是否合法，不查询账户余额，
    也不修改任何数据库对象。

    校验顺序：
    1. 合约存在且处于可交易状态；
    2. 当前版本只接受限价开仓；
    3. 价格和数量必须为正数；
    4. 数量符合合约最小、最大下单量；
    5. 价格符合合约最小变动价位。
    """

    @classmethod
    def validate_open_order(
        cls,
        *,
        request: OrderCreateRequest,
        instrument: Instrument | None,
    ) -> None:
        """校验一笔限价开仓订单。"""

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

        # 第一阶段不接收市价单、条件单等其他订单类型。
        if request.order_type != OrderType.LIMIT:
            raise BusinessValidationError(
                "当前只支持限价单",
                error_code="UNSUPPORTED_ORDER_TYPE",
            )

        # 平仓依赖持仓与冻结持仓模块，当前阶段明确拒绝。
        if request.offset_flag != OffsetFlag.OPEN:
            raise BusinessValidationError(
                "当前只支持开仓订单",
                error_code="UNSUPPORTED_OFFSET_FLAG",
            )

        if request.limit_price <= Decimal("0"):
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
            price=request.limit_price,
            price_tick=instrument.price_tick,
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
