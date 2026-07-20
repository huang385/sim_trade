from collections.abc import Callable
from datetime import date
from typing import Sequence
from uuid import uuid4

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.common.exceptions import (
    AppError,
    DataAccessError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from app.common.decimal_utils import quantize_money
from app.common.time_utils import utc_now
from app.enums.order_enums import OrderStatus, OrderSubmitStatus
from app.models.order import Order
from app.repositories.account_repository import AccountRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.outbox_repository import OutboxRepository
from app.schemas.order_schema import OrderCreateRequest
from app.services.fee_calculator import FeeCalculator
from app.services.margin_calculator import MarginCalculator
from app.services.order_freeze_service import OrderFreezeService
from app.services.order_validation_service import OrderValidationService
from app.services.rule_query_service import (
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


def decimal_to_json_string(value) -> str:
    """把金额按数据库精度转换为 JSON 字符串，禁止转成 float。"""

    return format(quantize_money(value), "f")


class OrderService:
    """
    限价开仓订单接收、校验、资金冻结和落库的事务入口。

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
        trading_day_provider: Callable[[], date] = date.today,
        order_id_factory: Callable[[], str] = generate_order_id,
        event_id_factory: Callable[[], str] = generate_event_id,
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
        self.trading_day_provider = trading_day_provider
        self.order_id_factory = order_id_factory
        self.event_id_factory = event_id_factory

    def create_order(
        self,
        db: Session,
        request: OrderCreateRequest,
    ) -> Order:
        """
        创建并接受一笔限价开仓订单。

        返回已有订单也属于成功，用于支持客户端在网络超时后
        使用相同 client_order_id 安全重试。
        """

        try:
            # 第一次幂等检查：常规重复请求无需查询规则或锁定账户。
            existing = self.order_repository.get_by_client_order_id(
                db=db,
                account_id=request.account_id,
                client_order_id=request.client_order_id,
            )
            if existing is not None:
                return existing

            # 当前阶段使用接单当天作为交易日。
            # 后续接入交易日历后，只需替换 trading_day_provider。
            trading_day = self.trading_day_provider()

            # 统一查询合约、当前保证金规则和当前手续费规则。
            # 三类参考数据必须全部存在并属于当前交易日。
            rules = self.rule_query_service.get_order_rules(
                db=db,
                exchange_id=request.exchange_id,
                symbol=request.symbol,
                trading_day=trading_day,
            )

            # 校验订单类型、开平标志、价格档位和数量范围。
            self.validation_service.validate_open_order(
                request=request,
                instrument=rules.instrument,
            )

            # 根据买卖方向选择多头或空头保证金率。
            frozen_margin = (
                self.margin_calculator.calculate_open_margin(
                    price=request.limit_price,
                    volume=request.volume,
                    direction=request.direction,
                    instrument=rules.instrument,
                    margin_rule=rules.margin_rule,
                )
            )
            # 按手续费规则计算开仓预计手续费。
            frozen_commission = self.fee_calculator.calculate(
                price=request.limit_price,
                volume=request.volume,
                offset_flag=request.offset_flag,
                instrument=rules.instrument,
                fee_rule=rules.fee_rule,
            )

            # SELECT FOR UPDATE 锁定账户。
            # 从这里开始，同一账户的其他下单事务需要等待本事务结束。
            account = (
                self.account_repository.get_by_account_id_for_update(
                    db=db,
                    account_id=request.account_id,
                )
            )

            # 同一账户的并发请求会在此处串行化。锁定后再次检查，
            # 避免两个相同 client_order_id 的请求重复冻结资金。
            existing = self.order_repository.get_by_client_order_id(
                db=db,
                account_id=request.account_id,
                client_order_id=request.client_order_id,
            )
            if existing is not None:
                return existing

            # 检查账户状态和可用资金，并修改冻结相关字段。
            self.freeze_service.freeze_open_order(
                account=account,
                frozen_margin=frozen_margin,
                frozen_commission=frozen_commission,
            )

            # 订单已完成所有校验和资源冻结，可以进入 ACCEPTED 状态。
            accepted_at = utc_now()
            order = self.order_repository.create(
                db=db,
                order_id=self.order_id_factory(),
                client_order_id=request.client_order_id,
                account_id=request.account_id,
                order_book_id=rules.instrument.order_book_id,
                symbol=rules.instrument.symbol,
                exchange_id=rules.instrument.exchange_id,
                trading_day=trading_day,
                direction=request.direction.value,
                offset_flag=request.offset_flag.value,
                order_type=request.order_type.value,
                limit_price=request.limit_price,
                total_volume=request.volume,
                status=OrderStatus.ACCEPTED.value,
                submit_status=OrderSubmitStatus.ACCEPTED.value,
                frozen_margin=frozen_margin,
                frozen_commission=frozen_commission,
                created_at=accepted_at,
                accepted_at=accepted_at,
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
                    "accepted_at": accepted_at.isoformat(),
                },
                created_at=accepted_at,
            )

            # 账户冻结、订单记录和 Outbox 事件在同一次 commit 中原子提交。
            # commit 失败时由下方异常分支统一 rollback。
            db.commit()
            db.refresh(order)
            return order

        except IntegrityError as exc:
            # 唯一键冲突可能来自极端并发幂等请求。
            # 回滚失败事务后再次查询，存在原订单则直接返回。
            db.rollback()
            existing = self.order_repository.get_by_client_order_id(
                db=db,
                account_id=request.account_id,
                client_order_id=request.client_order_id,
            )
            if existing is not None:
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
    ) -> Sequence[Order]:
        """查询指定账户的订单列表。"""

        return self.order_repository.list_by_account(
            db=db,
            account_id=account_id.strip(),
        )


def get_order_service() -> OrderService:
    """创建供 FastAPI Depends 使用的订单服务。"""

    return OrderService(
        order_repository=OrderRepository(),
        account_repository=AccountRepository(),
        rule_query_service=get_rule_query_service(),
        validation_service=OrderValidationService(),
        freeze_service=OrderFreezeService(),
        margin_calculator=MarginCalculator(),
        fee_calculator=FeeCalculator(),
        outbox_repository=OutboxRepository(),
    )
