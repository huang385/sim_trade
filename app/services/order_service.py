from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Sequence
from uuid import uuid4

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from redis.exceptions import RedisError

from app.common.exceptions import (
    AppError,
    BusinessRuleError,
    DataAccessError,
    ResourceConflictError,
    ResourceNotFoundError,
    ServiceUnavailableError,
)
from app.common.decimal_utils import quantize_money
from app.common.pagination_cursor import decode_cursor, encode_cursor
from app.common.time_utils import utc_now
from app.enums.order_enums import (
    OffsetFlag,
    OrderDirection,
    OrderStatus,
    OrderSubmitStatus,
    PositionDirection,
    PositionFreezeAllocationStatus,
)
from app.enums.reference_data_enums import CommissionType
from app.enums.option_enums import (
    InstrumentType,
    MarginPriceMode,
    OptionType,
)
from app.models.order import Order
from app.models.account import Account
from app.models.position_freeze_allocation import PositionFreezeAllocation
from app.repositories.account_repository import AccountRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.outbox_repository import OutboxRepository
from app.repositories.position_freeze_allocation_repository import (
    PositionFreezeAllocationRepository,
)
from app.repositories.position_repository import PositionRepository
from app.schemas.order_schema import (
    OrderCreateRequest,
    OrderPageResponse,
)
from app.services.fee_calculator import (
    FeeBucketEntry,
    FeeBucketKey,
    FeeCalculator,
)
from app.services.account_access_scope import AccountAccessScope
from app.services.account_risk_state_service import AccountRiskStateService
from app.services.settlement_gate_service import SettlementGateService
from app.enums.risk_enums import OrderSource
from app.services.margin_calculator import MarginCalculator
from app.services.option_margin_calculator import (
    OptionMarginInput,
    OptionMarginRuleSnapshot,
)
from app.services.option_margin_calculator_resolver import (
    OptionMarginCalculatorResolver,
)
from app.services.option_market_price_service import (
    OptionMarginMarketPrices,
    OptionMarketPriceService,
)
from app.infrastructure.market_pre_subscription_store import (
    MarketPreSubscriptionStore,
)
from app.services.option_premium_calculator import OptionPremiumCalculator
from app.services.option_trading_permission_service import (
    OptionTradingPermissionService,
)
from app.services.order_freeze_service import OrderFreezeService
from app.services.order_validation_service import OrderValidationService
from app.services.position_close_allocator import PositionCloseAllocator
from app.services.realtime_fact_event_service import RealtimeFactEventService
from app.services.rule_query_service import (
    OrderReferenceRules,
    RuleQueryService,
    get_rule_query_service,
)


def generate_order_id() -> str:
    """
    生成系统订单编号。

    编号由 UTC 日期和随机后缀组成，不依赖单机内存计数器，
    因而可以支持多个订单服务进程同时生成编号。
    数据库中的 order_id 唯一约束负责最终冲突保护。
    """

    day = utc_now().strftime("%Y%m%d")
    suffix = uuid4().hex[:16].upper()
    return f"O{day}{suffix}"


def generate_event_id() -> str:
    """
    生成全局唯一的事件编号。

    UUID 不依赖单个进程内的计数器，适合 API 和 Worker 多进程部署。
    数据库的 event_id 唯一约束继续提供最终冲突保护。
    """

    return f"EVT-{uuid4().hex.upper()}"


def generate_allocation_id() -> str:
    """生成平仓订单逐笔持仓冻结分配编号。"""

    return f"PFA{utc_now().strftime('%Y%m%d')}{uuid4().hex[:16].upper()}"


def decimal_to_json_string(value) -> str:
    """把金额按数据库精度转换为 JSON 字符串，禁止转成 float。"""

    return format(quantize_money(value), "f")


class OrderService:
    """
    期货限价开平仓订单接收、资源冻结和落库的事务入口。

    OrderService 负责串联各个单一职责组件：
    1. OrderRepository 负责订单查询与写入；
    2. RuleQueryService 负责合约和交易规则查询；
    3. OrderValidationService 负责价格、数量和订单类型校验；
    4. MarginCalculator 和 FeeCalculator 负责纯金额计算；
    5. AccountRepository 负责锁定账户；
    6. OrderFreezeService 负责检查和修改账户冻结字段。

    资金冻结和订单写入共享同一个 Session 和事务，
    任一环节失败时必须执行 rollback。
    """

    def __init__(
        self,
        *,
        order_repository: OrderRepository,
        account_repository: AccountRepository,
        rule_query_service: RuleQueryService,
        validation_service: OrderValidationService,
        freeze_service: OrderFreezeService,
        margin_calculator: MarginCalculator,
        fee_calculator: FeeCalculator,
        outbox_repository: OutboxRepository | None = None,
        position_repository: PositionRepository | None = None,
        allocation_repository: PositionFreezeAllocationRepository | None = None,
        close_allocator: PositionCloseAllocator | None = None,
        trading_day_provider: Callable[[], date] | None = None,
        trading_day_service=None,
        order_id_factory: Callable[[], str] = generate_order_id,
        event_id_factory: Callable[[], str] = generate_event_id,
        allocation_id_factory: Callable[[], str] = generate_allocation_id,
        default_access_scope: AccountAccessScope | None = None,
        option_permission_service: OptionTradingPermissionService | None = None,
        option_premium_calculator: OptionPremiumCalculator | None = None,
        option_margin_resolver: OptionMarginCalculatorResolver | None = None,
        option_market_price_service: OptionMarketPriceService | None = None,
        market_pre_subscription_store: MarketPreSubscriptionStore | None = None,
        settlement_gate_service: SettlementGateService | None = None,
    ):
        # 依赖通过构造函数传入，方便单元测试替换为 Mock，
        # 也便于未来迁移到更完整的依赖注入容器。
        self.order_repository = order_repository
        self.account_repository = account_repository
        self.rule_query_service = rule_query_service
        self.validation_service = validation_service
        self.freeze_service = freeze_service
        self.margin_calculator = margin_calculator
        self.fee_calculator = fee_calculator
        self.outbox_repository = outbox_repository or OutboxRepository()
        self.position_repository = position_repository or PositionRepository()
        self.allocation_repository = (
            allocation_repository or PositionFreezeAllocationRepository()
        )
        self.close_allocator = close_allocator or PositionCloseAllocator()
        self.trading_day_provider = trading_day_provider
        self.trading_day_service = trading_day_service
        self.order_id_factory = order_id_factory
        self.event_id_factory = event_id_factory
        self.realtime_fact_events = RealtimeFactEventService(
            repository=self.outbox_repository,
        )
        self.allocation_id_factory = allocation_id_factory
        # 仅测试或受信任内部调用可在构造时显式提供管理员范围。生产HTTP
        # Service不设置默认值，每次请求必须传入由current_user构造的范围。
        self.default_access_scope = default_access_scope
        self.option_permission_service = (
            option_permission_service or OptionTradingPermissionService()
        )
        self.option_premium_calculator = (
            option_premium_calculator or OptionPremiumCalculator()
        )
        self.option_margin_resolver = (
            option_margin_resolver or OptionMarginCalculatorResolver()
        )
        self.option_market_price_service = option_market_price_service
        self.market_pre_subscription_store = market_pre_subscription_store
        self.settlement_gate_service = (
            settlement_gate_service or SettlementGateService()
        )

    def _get_option_margin_prices(
        self,
        *,
        request: OrderCreateRequest,
        rules: OrderReferenceRules,
        authorized_account: Account,
    ) -> OptionMarginMarketPrices:
        """读取卖方保证金价格；首次缺行情时自动登记临时订阅需求。"""

        if (
            self.option_market_price_service is None
            or rules.underlying_instrument is None
        ):
            raise DataAccessError(
                "期权保证金行情上下文不完整",
                error_code="OPTION_MARGIN_CONTEXT_INCOMPLETE",
            )
        try:
            return self.option_market_price_service.get_margin_prices(
                option_instrument=rules.instrument,
                underlying_instrument=rules.underlying_instrument,
                order_limit_price=request.limit_price,
            )
        except BusinessRuleError as exc:
            if (
                exc.error_code != "OPTION_MARKET_PRICE_UNAVAILABLE"
                or self.market_pre_subscription_store is None
            ):
                raise
            # 兼容尚未调用准备接口的旧客户端：首次卖出开仓发现行情缺失时，
            # 自动写入“期权+标的”临时需求。请求本身不创建订单、不冻结资金，
            # 客户端等待行情就绪后使用同一client_order_id安全重试即可。
            self.option_permission_service.validate(
                account=authorized_account,
                instrument=rules.instrument,
                direction=request.direction,
                offset_flag=request.offset_flag,
            )
            try:
                self.market_pre_subscription_store.request_codes(
                    account_id=request.account_id,
                    codes={
                        rules.instrument.order_book_id,
                        rules.underlying_instrument.order_book_id,
                    },
                )
            except RedisError as redis_exc:
                raise ServiceUnavailableError(
                    "行情预订阅服务暂不可用",
                    error_code="MARKET_PRE_SUBSCRIPTION_UNAVAILABLE",
                ) from redis_exc
            raise ServiceUnavailableError(
                "期权和标的行情正在准备，请稍后重试下单",
                error_code="OPTION_MARKET_DATA_PREPARING",
            ) from exc

    def create_order(
        self,
        db: Session,
        request: OrderCreateRequest,
        *,
        access_scope: AccountAccessScope | None = None,
        order_source: str = OrderSource.USER.value,
        liquidation_task_id: str | None = None,
        reduce_only: bool = False,
    ) -> Order:
        """
        创建并接受一笔限价开仓或平仓订单。

        返回已有订单也属于成功，用于支持客户端在网络超时后
        使用相同 client_order_id 安全重试。
        """

        scope = access_scope or self.default_access_scope
        if scope is None:
            raise ValueError("创建订单必须显式提供账户授权范围")

        # 强平元数据只允许受信任的内部调用传入，HTTP请求Schema不暴露这些字段。
        if order_source == OrderSource.LIQUIDATION.value:
            if not liquidation_task_id or not reduce_only:
                raise ValueError("强平订单必须携带task_id并设置reduce_only")
            if request.offset_flag == OffsetFlag.OPEN:
                raise ValueError("强平订单不得使用OPEN")
        elif liquidation_task_id is not None or reduce_only:
            raise ValueError("普通用户订单不得伪造强平元数据")

        try:
            # 安全边界优先于规则错误：先以无锁查询确认账户存在和归属，
            # 未授权请求不会读取合约、保证金或手续费规则，更不会锁他人账户。
            authorized_account = (
                self.account_repository.get_by_account_id(
                    db,
                    request.account_id,
                )
                if scope.is_admin
                else self.account_repository.get_owned_account(
                    db,
                    account_id=request.account_id,
                    user_id=scope.user_id,
                    for_update=False,
                )
            )
            if authorized_account is None:
                if scope.conceal_resource_existence:
                    raise ResourceNotFoundError(
                        "目标资源不存在",
                        error_code="RESOURCE_NOT_FOUND",
                    )
                raise ResourceNotFoundError(
                    "账户不存在",
                    error_code="ACCOUNT_NOT_FOUND",
                )

            # 第一轮幂等查询不加业务行锁。普通用户通过Account归属Join限制
            # 可见范围；命中后仍必须验证本次业务字段与原请求完全一致。
            existing = (
                self.order_repository.get_by_client_order_id(
                    db=db,
                    account_id=request.account_id,
                    client_order_id=request.client_order_id,
                )
                if scope.is_admin
                else self.order_repository.get_by_client_order_id_for_user(
                    db=db,
                    account_id=request.account_id,
                    client_order_id=request.client_order_id,
                    user_id=scope.user_id,
                )
            )
            if existing is not None:
                self.validation_service.validate_idempotent_order_request(
                    existing_order=existing,
                    request=request,
                )
                # 幂等命中不查询当前规则，也不锁账户；提交只结束本次只读
                # 事务，分离对象避免响应序列化触发额外SQL。
                db.expunge(existing)
                db.commit()
                return existing

            resolved_instrument = None
            if self.trading_day_provider is not None:
                # 测试和离线工具可注入确定性交易日；生产入口不使用自然日。
                trading_day = self.trading_day_provider()
            else:
                if self.trading_day_service is None:
                    raise RuntimeError("交易日服务未配置")
                resolved_instrument = self.rule_query_service.get_instrument(
                    db,
                    exchange_id=request.exchange_id,
                    symbol=request.symbol,
                )
                trading_day = self.trading_day_service.resolve_for_order(
                    db,
                    instrument=resolved_instrument,
                    offset_flag=request.offset_flag,
                )
            # advisory事务共享锁必须在任何资金或订单写入前取得。日终进程
            # 持有同键排他锁时，本事务会等待；等待结束后再检查数据库批次，
            # 从而消除“检查时未结算、随后并发落单”的竞态。
            self.settlement_gate_service.ensure_trading_open(
                db, trading_day=trading_day
            )

            # 统一查询合约、当前保证金规则和当前手续费规则。
            # 三类参考数据必须全部存在并属于当前交易日。
            rule_arguments = {
                "db": db,
                "exchange_id": request.exchange_id,
                "symbol": request.symbol,
                "trading_day": trading_day,
                "direction": request.direction.value,
                "offset_flag": request.offset_flag.value,
            }
            if resolved_instrument is not None:
                rule_arguments["instrument"] = resolved_instrument
            rules = self.rule_query_service.get_order_rules(**rule_arguments)

            # 校验订单类型、开平标志、价格档位和数量范围。
            self.validation_service.validate_order(
                request=request,
                instrument=rules.instrument,
            )

            is_open = request.offset_flag == OffsetFlag.OPEN
            instrument_type = getattr(
                rules.instrument,
                "instrument_type",
                InstrumentType.FUTURES.value,
            )
            is_option = instrument_type in {
                InstrumentType.FUTURES_OPTION.value,
                InstrumentType.INDEX_OPTION.value,
            }
            fee_rule_id = None
            fee_rule_version = None
            fee_rule_snapshot = None
            if is_option:
                fee_item = rules.fee_rule_item
                if fee_item is None:
                    raise DataAccessError(
                        "期权手续费规则查询结果不完整",
                        error_code="OPTION_FEE_RULE_INCONSISTENT",
                    )
                commission_type = CommissionType(
                    fee_item.commission_type
                ).value
                commission_parameter = Decimal(
                    fee_item.commission_parameter
                )
                fee_rule_id = fee_item.id
                fee_rule_version = fee_item.rule_version
                fee_rule_snapshot = {
                    "rule_id": fee_item.id,
                    "rule_version": fee_item.rule_version,
                    "instrument_type": instrument_type,
                    "direction": request.direction.value,
                    "offset_flag": request.offset_flag.value,
                    "commission_type": commission_type,
                    "commission_parameter": format(
                        commission_parameter, "f"
                    ),
                    "data_source": fee_item.data_source,
                }
            else:
                if rules.fee_rule is None or rules.margin_rule is None:
                    raise DataAccessError(
                        "期货交易规则查询结果不完整",
                        error_code="FUTURES_RULE_INCONSISTENT",
                    )
                commission_type = CommissionType(
                    rules.fee_rule.commission_type
                ).value
                commission_parameter = (
                    self.fee_calculator.resolve_commission_parameter(
                        offset_flag=request.offset_flag,
                        fee_rule=rules.fee_rule,
                    )
                )
            commission_contract_multiplier = Decimal(
                rules.instrument.contract_multiplier
            )
            frozen_cash = Decimal("0.000000")
            margin_rule_id = None
            margin_rule_version = None
            margin_price_mode = None
            margin_underlying_price = None
            margin_option_price = None
            margin_rule_snapshot = None
            margin_snapshot_schema_version = None
            margin_calculation_version = None
            underlying_order_book_id = None
            underlying_exchange_id = None
            underlying_symbol = None
            if is_option and rules.underlying_instrument is not None:
                underlying_order_book_id = (
                    rules.underlying_instrument.order_book_id
                )
                underlying_exchange_id = rules.underlying_instrument.exchange_id
                underlying_symbol = rules.underlying_instrument.symbol
            # 只有开仓订单需要新增保证金；平仓订单只冻结手续费和持仓。
            frozen_margin = (
                self.margin_calculator.calculate_open_margin(
                    price=request.limit_price,
                    volume=request.volume,
                    direction=request.direction,
                    instrument=rules.instrument,
                    margin_rule=rules.margin_rule,
                )
                if is_open and not is_option
                else Decimal("0.000000")
            )
            if is_option and request.direction == OrderDirection.BUY:
                frozen_cash = self.option_premium_calculator.calculate(
                    price=request.limit_price,
                    volume=request.volume,
                    multiplier=commission_contract_multiplier,
                )
            if (
                is_option
                and request.direction == OrderDirection.SELL
                and is_open
            ):
                if (
                    rules.option_margin_rule is None
                    or rules.underlying_instrument is None
                    or self.option_market_price_service is None
                ):
                    raise DataAccessError(
                        "期权保证金计算上下文不完整",
                        error_code="OPTION_MARGIN_CONTEXT_INCOMPLETE",
                    )
                if (
                    instrument_type == InstrumentType.FUTURES_OPTION.value
                    and rules.underlying_margin_rule is None
                ):
                    raise DataAccessError(
                        "商品期权标的保证金规则不完整",
                        error_code="OPTION_MARGIN_CONTEXT_INCOMPLETE",
                    )
                prices = self._get_option_margin_prices(
                    request=request,
                    rules=rules,
                    authorized_account=authorized_account,
                )
                option_rule = rules.option_margin_rule
                rule_snapshot = OptionMarginRuleSnapshot(
                    rule_id=option_rule.id,
                    rule_version=option_rule.rule_version,
                    margin_algorithm=option_rule.margin_algorithm,
                    margin_adjustment_rate=Decimal(
                        option_rule.margin_adjustment_rate
                    ),
                    minimum_guarantee_rate=Decimal(
                        option_rule.minimum_guarantee_rate
                    ),
                    out_of_money_deduction_rate=Decimal(
                        option_rule.out_of_money_deduction_rate
                    ),
                    minimum_underlying_margin_ratio=Decimal(
                        option_rule.minimum_underlying_margin_ratio
                    ),
                    extra_margin_rate=Decimal(
                        option_rule.extra_margin_rate
                    ),
                )
                underlying_multiplier = Decimal(
                    rules.underlying_instrument.contract_multiplier
                )
                underlying_margin_rate = Decimal("0")
                underlying_margin_per_lot = Decimal("0.000000")
                # 商品期权公式需要标的期货每手保证金；股指期权公式直接
                # 使用指数点位，因此不查询也不伪造标的保证金规则。
                if instrument_type == InstrumentType.FUTURES_OPTION.value:
                    underlying_margin_rate = max(
                        Decimal(
                            rules.underlying_margin_rule.long_margin_rate
                        ),
                        Decimal(
                            rules.underlying_margin_rule.short_margin_rate
                        ),
                    )
                    underlying_margin_per_lot = quantize_money(
                        prices.underlying_price
                        * underlying_multiplier
                        * underlying_margin_rate
                    )
                calculator = self.option_margin_resolver.resolve(
                    instrument_type=instrument_type,
                    exchange_id=rules.instrument.exchange_id,
                    margin_algorithm=option_rule.margin_algorithm,
                )
                margin_result = calculator.calculate(
                    OptionMarginInput(
                        option_type=OptionType(
                            rules.instrument.option_type
                        ),
                        strike_price=Decimal(
                            rules.instrument.strike_price
                        ),
                        option_price=prices.option_price,
                        underlying_price=prices.underlying_price,
                        option_multiplier=commission_contract_multiplier,
                        underlying_multiplier=underlying_multiplier,
                        volume=request.volume,
                        price_mode=MarginPriceMode.ORDER_FREEZE,
                        calculated_at=utc_now(),
                        rule=rule_snapshot,
                        underlying_margin_per_lot=(
                            underlying_margin_per_lot
                        ),
                    )
                )
                frozen_margin = margin_result.total_margin
                margin_rule_id = margin_result.rule_id
                margin_rule_version = margin_result.rule_version
                margin_price_mode = margin_result.price_mode.value
                margin_underlying_price = (
                    margin_result.underlying_price
                )
                margin_option_price = margin_result.option_price
                margin_rule_snapshot = rule_snapshot.to_json_mapping()
                if instrument_type == InstrumentType.FUTURES_OPTION.value:
                    # 商品期权实时保证金需要重放标的期货每手保证金，
                    # 因此固化当次使用的保证金率与乘数。
                    margin_rule_snapshot.update(
                        {
                            "underlying_margin_rate": format(
                                underlying_margin_rate, "f"
                            ),
                            "underlying_multiplier": format(
                                underlying_multiplier, "f"
                            ),
                        }
                    )
                margin_snapshot_schema_version = "1"
                margin_calculation_version = (
                    margin_result.calculation_version
                )
            # 开仓可以直接按整张订单计算；平仓必须先完成逐笔持仓分配，
            # 再按每条 Allocation 的最终平今/平昨标志分别计算。
            frozen_commission = (
                self.fee_calculator.calculate_from_snapshot(
                    price=request.limit_price,
                    volume=request.volume,
                    commission_type=commission_type,
                    commission_parameter=commission_parameter,
                    contract_multiplier=commission_contract_multiplier,
                )
                if is_open
                else Decimal("0.000000")
            )

            # 规则读取、订单校验及开仓金额纯计算完成后才锁定账户，缩短同一
            # 账户并发下单的行锁持有时间。普通用户把user_id直接放入锁定
            # SQL，未授权请求不会锁住其他用户账户；管理员使用无范围锁。
            account = (
                self.account_repository.get_by_account_id_for_update(
                    db=db,
                    account_id=request.account_id,
                )
                if scope.is_admin
                else self.account_repository.get_owned_account_for_update(
                    db=db,
                    account_id=request.account_id,
                    user_id=scope.user_id,
                )
            )
            if account is None:
                if scope.conceal_resource_existence:
                    raise ResourceNotFoundError(
                        "目标资源不存在",
                        error_code="RESOURCE_NOT_FOUND",
                    )
                raise ResourceNotFoundError(
                    "账户不存在",
                    error_code="ACCOUNT_NOT_FOUND",
                )
            # 两个同client_order_id请求可能同时通过第一轮无锁查询。账户锁
            # 串行化后必须再次检查，保证只冻结一次并只创建一笔订单。
            existing = self.order_repository.get_by_client_order_id(
                db=db,
                account_id=request.account_id,
                client_order_id=request.client_order_id,
            )
            if existing is not None:
                self.validation_service.validate_idempotent_order_request(
                    existing_order=existing,
                    request=request,
                )
                db.expunge(existing)
                db.commit()
                return existing

            # 账户不存在或不可交易应先于持仓查询返回，避免错误地报告
            # POSITION_NOT_FOUND。实际资金修改仍由对应冻结分支完成。
            self.freeze_service.validate_account_tradable(account)
            if account.trading_day != trading_day:
                raise BusinessRuleError(
                    "账户交易日与当前交易时段不一致，请先完成日终结算",
                    error_code="ACCOUNT_TRADING_DAY_MISMATCH",
                )
            # OPEN会增加账户风险，必须在账户行锁内、各品种权限检查之前统一拦截。
            # 这样期货、商品期权和股指期权都会返回同一套风险错误码；平仓订单
            # 不经过该检查，因此在风险状态下仍可用于降低敞口。
            if is_open:
                AccountRiskStateService.ensure_open_allowed(
                    getattr(account, "risk_state", "NORMAL")
                )
            self.option_permission_service.validate(
                account=account,
                instrument=rules.instrument,
                direction=request.direction,
                offset_flag=request.offset_flag,
            )

            order_id = self.order_id_factory()
            accepted_at = utc_now()
            frozen_position_volume = 0
            position = None

            if is_open:
                if is_option:
                    self.freeze_service.freeze_option_resources(
                        account=account,
                        frozen_cash=frozen_cash,
                        frozen_margin=frozen_margin,
                        frozen_commission=frozen_commission,
                    )
                else:
                    self.freeze_service.freeze_open_order(
                        account=account,
                        frozen_margin=frozen_margin,
                        frozen_commission=frozen_commission,
                    )
            else:
                # SELL平多、BUY平空，方向必须和开平标志共同解释。
                position_direction = (
                    PositionDirection.LONG.value
                    if request.direction == OrderDirection.SELL
                    else PositionDirection.SHORT.value
                )
                position = self.position_repository.get_for_update(
                    db,
                    account_id=request.account_id,
                    exchange_id=rules.instrument.exchange_id,
                    symbol=rules.instrument.symbol,
                    direction=position_direction,
                )
                if position is None:
                    raise ResourceNotFoundError(
                        "可平持仓不存在",
                        error_code="POSITION_NOT_FOUND",
                    )
                details = (
                    self.position_repository.list_available_details_for_update(
                        db,
                        position_id=position.position_id,
                    )
                )
                plans = self.close_allocator.allocate(
                    details=details,
                    offset_flag=request.offset_flag,
                    trading_day=trading_day,
                    volume=request.volume,
                )
                allocation_fee_metadata = []
                for plan in plans:
                    if is_option:
                        allocation_fee_rule = (
                            self.rule_query_service.get_option_fee_rule(
                                db,
                                instrument=rules.instrument,
                                direction=request.direction.value,
                                offset_flag=(
                                    plan.resolved_offset_flag.value
                                ),
                                trading_day=trading_day,
                            )
                        )
                        allocation_type = CommissionType(
                            allocation_fee_rule.commission_type
                        ).value
                        allocation_parameter = Decimal(
                            allocation_fee_rule.commission_parameter
                        )
                    else:
                        allocation_type = commission_type
                        allocation_parameter = (
                            self.fee_calculator
                            .resolve_commission_parameter(
                                offset_flag=plan.resolved_offset_flag,
                                fee_rule=rules.fee_rule,
                            )
                        )
                    allocation_fee_metadata.append(
                        (
                            plan,
                            allocation_type,
                            allocation_parameter,
                        )
                    )
                # 相同平今/平昨及相同规则快照组成一个手续费桶。每个桶只按
                # 总数量计算一次，避免 PositionDetail 数量改变订单手续费。
                allocation_commissions = (
                    self.fee_calculator.calculate_bucket_allocations(
                        price=request.limit_price,
                        entries=[
                            FeeBucketEntry(
                                key=FeeBucketKey(
                                    resolved_offset_flag=(
                                        plan.resolved_offset_flag.value
                                    ),
                                    commission_type=allocation_type,
                                    commission_parameter=parameter,
                                    commission_contract_multiplier=(
                                        commission_contract_multiplier
                                    ),
                                ),
                                volume=plan.volume,
                            )
                            for (
                                plan,
                                allocation_type,
                                parameter,
                            ) in allocation_fee_metadata
                        ],
                    )
                )
                allocation_fee_snapshots = [
                    (plan, allocation_type, parameter, commission)
                    for (
                        plan,
                        allocation_type,
                        parameter,
                    ), commission in zip(
                        allocation_fee_metadata,
                        allocation_commissions,
                        strict=True,
                    )
                ]
                frozen_commission = quantize_money(
                    sum(
                        (
                            item[3]
                            for item in allocation_fee_snapshots
                        ),
                        Decimal("0"),
                    )
                )
                if is_option:
                    self.freeze_service.freeze_option_resources(
                        account=account,
                        frozen_cash=frozen_cash,
                        frozen_margin=Decimal("0"),
                        frozen_commission=frozen_commission,
                    )
                else:
                    self.freeze_service.freeze_close_order_commission(
                        account=account,
                        frozen_commission=frozen_commission,
                    )
                for (
                    plan,
                    allocation_type,
                    allocation_parameter,
                    allocation_frozen_commission,
                ) in allocation_fee_snapshots:
                    plan.detail.frozen_volume += plan.volume
                    plan.detail.updated_at = accepted_at
                    self.allocation_repository.add(
                        db,
                        PositionFreezeAllocation(
                            allocation_id=self.allocation_id_factory(),
                            order_id=order_id,
                            position_id=position.position_id,
                            position_detail_id=plan.detail.position_detail_id,
                            account_id=request.account_id,
                            exchange_id=rules.instrument.exchange_id,
                            symbol=rules.instrument.symbol,
                            offset_flag=request.offset_flag.value,
                            resolved_offset_flag=(
                                plan.resolved_offset_flag.value
                            ),
                            commission_type=allocation_type,
                            commission_parameter=allocation_parameter,
                            commission_contract_multiplier=(
                                commission_contract_multiplier
                            ),
                            original_frozen_volume=plan.volume,
                            remaining_frozen_volume=plan.volume,
                            consumed_volume=0,
                            released_volume=0,
                            original_frozen_commission=(
                                allocation_frozen_commission
                            ),
                            remaining_frozen_commission=(
                                allocation_frozen_commission
                            ),
                            consumed_commission=Decimal("0.000000"),
                            released_commission=Decimal("0.000000"),
                            status=PositionFreezeAllocationStatus.ACTIVE.value,
                            created_at=accepted_at,
                            updated_at=accepted_at,
                        ),
                    )
                position.frozen_volume += request.volume
                position.available_volume -= request.volume
                position.updated_at = accepted_at
                frozen_position_volume = request.volume

            # 账户冻结字段和可用资金已经发生变化，更新时间必须与本次接单
            # 事务一致，供实时账户绝对事实和审计查询共同使用。
            account.updated_at = accepted_at

            # 订单已完成所有校验和资源冻结，可以进入ACCEPTED状态。
            order = self.order_repository.create(
                db=db,
                order_id=order_id,
                client_order_id=request.client_order_id,
                account_id=request.account_id,
                order_book_id=rules.instrument.order_book_id,
                symbol=rules.instrument.symbol,
                exchange_id=rules.instrument.exchange_id,
                trading_day=trading_day,
                instrument_type=instrument_type,
                underlying_order_book_id=underlying_order_book_id,
                underlying_exchange_id=underlying_exchange_id,
                underlying_symbol=underlying_symbol,
                direction=request.direction.value,
                offset_flag=request.offset_flag.value,
                order_type=request.order_type.value,
                commission_type=commission_type,
                commission_parameter=commission_parameter,
                commission_contract_multiplier=(
                    commission_contract_multiplier
                ),
                fee_rule_id=fee_rule_id,
                fee_rule_version=fee_rule_version,
                fee_rule_snapshot=fee_rule_snapshot,
                limit_price=request.limit_price,
                total_volume=request.volume,
                status=OrderStatus.ACCEPTED.value,
                submit_status=OrderSubmitStatus.ACCEPTED.value,
                frozen_margin=frozen_margin,
                frozen_cash=frozen_cash,
                frozen_commission=frozen_commission,
                frozen_position_volume=frozen_position_volume,
                created_at=accepted_at,
                accepted_at=accepted_at,
                margin_rule_id=margin_rule_id,
                margin_rule_version=margin_rule_version,
                margin_price_mode=margin_price_mode,
                margin_underlying_price=margin_underlying_price,
                margin_option_price=margin_option_price,
                margin_rule_snapshot=margin_rule_snapshot,
                margin_snapshot_schema_version=(
                    margin_snapshot_schema_version
                ),
                margin_calculation_version=margin_calculation_version,
                order_source=order_source,
                liquidation_task_id=liquidation_task_id,
                reduce_only=reduce_only,
            )

            # ORDER_ACCEPTED 事件与订单、账户冻结共用当前 Session。
            # 这里只写 PostgreSQL，不在 HTTP 请求内访问 Redis，因而 Redis
            # 不可用不会阻断下单，也不会造成业务事务和消息状态不一致。
            event_id = self.event_id_factory()
            self.outbox_repository.create_event(
                db=db,
                event_id=event_id,
                aggregate_type="ORDER",
                aggregate_id=order.order_id,
                event_type="ORDER_ACCEPTED",
                payload={
                    "event_id": event_id,
                    "event_type": "ORDER_ACCEPTED",
                    "order_id": order.order_id,
                    "account_id": request.account_id,
                    "client_order_id": request.client_order_id,
                    "exchange_id": rules.instrument.exchange_id,
                    "symbol": rules.instrument.symbol,
                    "order_book_id": rules.instrument.order_book_id,
                    "trading_day": trading_day.isoformat(),
                    "direction": request.direction.value,
                    "offset_flag": request.offset_flag.value,
                    "order_type": request.order_type.value,
                    "limit_price": decimal_to_json_string(
                        request.limit_price
                    ),
                    "total_volume": request.volume,
                    "remaining_volume": request.volume,
                    "frozen_margin": decimal_to_json_string(
                        frozen_margin
                    ),
                    "frozen_cash": decimal_to_json_string(frozen_cash),
                    "frozen_commission": decimal_to_json_string(
                        frozen_commission
                    ),
                    "frozen_position_volume": frozen_position_volume,
                    "order_source": order_source,
                    "liquidation_task_id": liquidation_task_id,
                    "reduce_only": reduce_only,
                    "accepted_at": accepted_at.isoformat(),
                },
                created_at=accepted_at,
            )

            self.realtime_fact_events.create_account_updated(
                db,
                account=account,
                occurred_at=accepted_at,
                account_id=request.account_id,
            )
            if position is not None:
                # 平仓接单会冻结持仓；即使尚未成交，客户端也必须看到最新
                # available_volume和frozen_volume，而不是等待完整快照。
                self.realtime_fact_events.create_position_updated(
                    db,
                    position=position,
                    occurred_at=accepted_at,
                )

            # 账户冻结、订单记录和 Outbox 事件在同一次 commit 中原子提交。
            # commit 失败时由下方异常分支统一 rollback。
            db.commit()
            db.refresh(order)
            return order

        except IntegrityError as exc:
            # 唯一键冲突可能来自极端并发幂等请求。
            # 回滚失败事务后必须按当前用户范围恢复幂等结果，不能越权返回
            # 其他用户账户中的订单。
            db.rollback()
            existing = (
                self.order_repository.get_by_client_order_id(
                    db=db,
                    account_id=request.account_id,
                    client_order_id=request.client_order_id,
                )
                if scope.is_admin
                else self.order_repository.get_by_client_order_id_for_user(
                    db=db,
                    account_id=request.account_id,
                    client_order_id=request.client_order_id,
                    user_id=scope.user_id,
                )
            )
            if existing is not None:
                self.validation_service.validate_idempotent_order_request(
                    existing_order=existing,
                    request=request,
                )
                db.expunge(existing)
                db.commit()
                return existing
            raise ResourceConflictError(
                "订单编号冲突",
                error_code="ORDER_CONFLICT",
            ) from exc

        except AppError:
            # 业务异常保持原有错误类型和 error_code，
            # 但必须先释放账户锁并回滚可能的字段修改。
            db.rollback()
            raise

        except SQLAlchemyError as exc:
            # 数据库异常转换为统一的数据访问异常，避免向 API 泄露细节。
            db.rollback()
            raise DataAccessError(
                "创建订单失败",
                error_code="ORDER_CREATE_FAILED",
            ) from exc

        except Exception:
            # 未预料异常也必须回滚，但不在此处吞掉，便于日志定位。
            db.rollback()
            raise

    def get_order(
        self,
        db: Session,
        order_id: str,
    ) -> Order:
        """根据系统订单编号查询订单，不存在时抛出业务异常。"""

        order = self.order_repository.get_by_order_id(
            db=db,
            order_id=order_id.strip(),
        )
        if order is None:
            raise ResourceNotFoundError(
                "订单不存在",
                error_code="ORDER_NOT_FOUND",
            )
        return order

    def list_orders(
        self,
        db: Session,
        account_id: str,
        *,
        after_id: int | None = None,
        limit: int = 100,
    ) -> Sequence[Order]:
        """有界查询指定账户最近订单或游标后的增量订单。"""

        return self.order_repository.list_by_account(
            db=db,
            account_id=account_id.strip(),
            after_id=after_id,
            limit=limit,
        )

    def list_order_page(
        self,
        db: Session,
        account_id: str,
        *,
        cursor: str | None,
        limit: int,
    ) -> OrderPageResponse:
        """返回可供客户端继续请求下一页的不透明游标分页结果。"""

        normalized_account_id = account_id.strip()
        filters = {"account_id": normalized_account_id}
        before_id = None
        if cursor is not None:
            before_id = decode_cursor(
                cursor,
                expected_kind="orders",
                expected_filters=filters,
            ).before_id
        rows = list(
            self.order_repository.list_page_by_account(
                db,
                normalized_account_id,
                before_id=before_id,
                fetch_size=limit + 1,
            )
        )
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = (
            encode_cursor(
                kind="orders",
                before_id=items[-1].id,
                filters=filters,
            )
            if has_more and items
            else None
        )
        return OrderPageResponse(
            items=items,
            next_cursor=next_cursor,
            has_more=has_more,
        )


def get_order_service() -> OrderService:
    """创建供 FastAPI Depends 使用的订单服务。"""

    from app.core.redis_client import redis_client
    from app.infrastructure.market_data.market_tick_store import (
        MarketTickStore,
    )
    from app.core.config import settings
    from app.services.trading_day_service import get_trading_day_service

    return OrderService(
        order_repository=OrderRepository(),
        account_repository=AccountRepository(),
        rule_query_service=get_rule_query_service(),
        validation_service=OrderValidationService(),
        freeze_service=OrderFreezeService(),
        margin_calculator=MarginCalculator(),
        fee_calculator=FeeCalculator(),
        outbox_repository=OutboxRepository(),
        position_repository=PositionRepository(),
        allocation_repository=PositionFreezeAllocationRepository(),
        close_allocator=PositionCloseAllocator(),
        trading_day_service=get_trading_day_service(),
        option_market_price_service=OptionMarketPriceService(
            MarketTickStore(redis_client)
        ),
        market_pre_subscription_store=MarketPreSubscriptionStore(
            redis_client,
            ttl_seconds=settings.market_pre_subscription_ttl_seconds,
            max_codes_per_account=(
                settings.market_pre_subscription_max_codes_per_account
            ),
        ),
    )
