from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Sequence
from uuid import uuid4

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.common.decimal_utils import quantize_money
from app.common.exceptions import DataAccessError, ResourceNotFoundError
from app.common.pagination_cursor import decode_cursor, encode_cursor
from app.common.time_utils import utc_now
from app.core.config import settings
from app.core.redis_client import redis_client
from app.enums.order_enums import (
    OffsetFlag,
    OrderDirection,
    OrderStatus,
    OrderType,
    PositionDetailStatus,
    PositionDirection,
)
from app.enums.account_enums import AccountRiskState
from app.matching.types import MatchResult
from app.models.position import Position
from app.models.position_detail import PositionDetail
from app.models.trade import Trade
from app.repositories.account_repository import AccountRepository
from app.repositories.instrument_repository import InstrumentRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.outbox_repository import OutboxRepository
from app.repositories.position_repository import PositionRepository
from app.repositories.position_freeze_allocation_repository import (
    PositionFreezeAllocationRepository,
)
from app.repositories.trade_repository import TradeRepository
from app.repositories.trade_position_allocation_repository import (
    TradePositionAllocationRepository,
)
from app.infrastructure.market_data.market_tick_store import MarketTickStore
from app.schemas.trade_schema import TradePageResponse
from app.services.close_trade_settlement_handler import (
    CloseTradeSettlementHandler,
)
from app.services.fee_calculator import FeeCalculator
from app.services.option_trade_settlement_strategy import (
    OptionTradeSettlementStrategy,
)
from app.services.option_order_margin_adjustment_service import (
    OptionOrderMarginAdjustmentService,
)
from app.services.realtime_fact_event_service import RealtimeFactEventService
from app.services.product_strategy_registry import (
    ProductFamily,
    ProductStrategyRegistry,
    product_strategy_registry,
)
from app.services.settlement_gate_service import SettlementGateService


@dataclass(frozen=True)
class SettlementCommand:
    """
    从撮合编排层交给成交结算层的完整命令。

    订单编号、行情事件和 Redis Stream 追踪信息属于业务上下文，不进入
    纯撮合模型；MatchResult 只描述价格和数量计算结果。
    """

    order_id: str
    market_event_id: str
    market_stream_message_id: str
    tick_event_time: datetime
    tick_sequence_id: int
    match_result: MatchResult


@dataclass(frozen=True)
class SettlementResult:
    """
    成交事务处理结果。

    该类型属于结算服务而不是纯撮合领域。幂等重放时返回原成交编号，
    订单失效或不存在时通过 action 向编排层说明无需继续处理。
    """

    trade_id: str | None
    order_id: str
    action: str


ACTIVE_ORDER_STATUSES = {
    OrderStatus.ACCEPTED.value,
    OrderStatus.PARTIALLY_FILLED.value,
}
SUPPORTED_OFFSET_FLAGS = {
    OffsetFlag.OPEN.value,
    OffsetFlag.CLOSE.value,
    OffsetFlag.CLOSE_TODAY.value,
    OffsetFlag.CLOSE_YESTERDAY.value,
}


def _generate_id(prefix: str) -> str:
    """生成多进程安全的业务编号，数据库唯一约束继续负责最终保护。"""

    return f"{prefix}{utc_now().strftime('%Y%m%d')}{uuid4().hex[:16].upper()}"


def _decimal_string(value: Decimal) -> str:
    """Outbox 中的金额使用六位小数字符串，严禁经由 float。"""

    return format(quantize_money(value), "f")


class TradeSettlementService:
    """
    把一次撮合结果原子结算为成交、资金和持仓变化。

    本服务拥有成交事务边界。Trade、Order、Account、Position、
    PositionDetail和Outbox必须全部成功或全部回滚，任何中间状态都不能
    对其他请求可见。Redis活动索引不在该事务中直接修改，而由Outbox事件
    异步驱动，从而避免数据库提交和Redis写入之间出现双写不一致。
    """

    def __init__(
        self,
        *,
        order_repository: OrderRepository | None = None,
        account_repository: AccountRepository | None = None,
        instrument_repository: InstrumentRepository | None = None,
        trade_repository: TradeRepository | None = None,
        position_repository: PositionRepository | None = None,
        allocation_repository: PositionFreezeAllocationRepository | None = None,
        close_handler: CloseTradeSettlementHandler | None = None,
        option_strategy: OptionTradeSettlementStrategy | None = None,
        option_order_margin_service: (
            OptionOrderMarginAdjustmentService | None
        ) = None,
        fee_calculator: FeeCalculator | None = None,
        outbox_repository: OutboxRepository | None = None,
        trade_id_factory: Callable[[], str] | None = None,
        position_id_factory: Callable[[], str] | None = None,
        position_detail_id_factory: Callable[[], str] | None = None,
        event_id_factory: Callable[[], str] | None = None,
        settlement_gate_service: SettlementGateService | None = None,
        product_registry: ProductStrategyRegistry | None = None,
    ):
        # 所有依赖都允许从构造函数注入，便于单元测试精确模拟某一步失败，
        # 同时生产环境默认使用真实Repository和UUID编号工厂。
        self.order_repository = order_repository or OrderRepository()
        self.account_repository = account_repository or AccountRepository()
        self.instrument_repository = instrument_repository or InstrumentRepository()
        self.trade_repository = trade_repository or TradeRepository()
        self.position_repository = position_repository or PositionRepository()
        self.allocation_repository = (
            allocation_repository or PositionFreezeAllocationRepository()
        )
        self.close_handler = close_handler or CloseTradeSettlementHandler(
            position_repository=self.position_repository,
            allocation_repository=self.allocation_repository,
            trade_repository=self.trade_repository,
        )
        self.option_strategy = option_strategy or OptionTradeSettlementStrategy()
        self.option_order_margin_service = (
            option_order_margin_service
            or OptionOrderMarginAdjustmentService(
                market_tick_store=MarketTickStore(
                    redis_client,
                    stream_name=settings.market_tick_stream_name,
                )
            )
        )
        self.fee_calculator = fee_calculator or FeeCalculator()
        self.outbox_repository = outbox_repository or OutboxRepository()
        self.trade_id_factory = trade_id_factory or (lambda: _generate_id("T"))
        self.position_id_factory = position_id_factory or (lambda: _generate_id("P"))
        self.position_detail_id_factory = position_detail_id_factory or (
            lambda: _generate_id("PD")
        )
        self.event_id_factory = event_id_factory or (
            lambda: f"EVT-{uuid4().hex.upper()}"
        )
        self.realtime_fact_events = RealtimeFactEventService(
            repository=self.outbox_repository,
        )
        self.settlement_gate_service = (
            settlement_gate_service or SettlementGateService()
        )
        self.product_registry = product_registry or product_strategy_registry

    @staticmethod
    def _allocate_frozen(
        remaining_amount: Decimal,
        *,
        fill_volume: int,
        remaining_volume_before_fill: int,
    ) -> Decimal:
        """按成交量分摊冻结资源，最后一笔直接吃完尾差。"""

        if fill_volume == remaining_volume_before_fill:
            return quantize_money(remaining_amount)
        return quantize_money(
            remaining_amount
            * Decimal(fill_volume)
            / Decimal(remaining_volume_before_fill)
        )

    @staticmethod
    def _is_matchable_order(order, result: MatchResult) -> bool:
        """
        在订单行锁内重新确认是否允许结算。

        撮合引擎使用的是行锁前读取的快照，等待锁期间订单可能已经被其他
        Tick部分成交或完全成交，因此不能直接相信引擎阶段的剩余量和状态。
        """

        return (
            order.status in ACTIVE_ORDER_STATUSES
            and order.remaining_volume > 0
            and order.order_type in {
                OrderType.LIMIT.value,
                OrderType.COUNTERPARTY.value,
                OrderType.LAST.value,
                OrderType.MARKET.value,
            }
            and order.offset_flag in SUPPORTED_OFFSET_FLAGS
            and result.fill_price is not None
            and result.fill_price > 0
            and result.fill_volume > 0
        )

    def _create_outbox_events(
        self,
        db: Session,
        *,
        trade: Trade,
        order,
        account,
        position: Position,
        now,
    ) -> None:
        """
        在成交事务内同时写入成交事件和订单状态事件。

        两个事件先进入PostgreSQL Outbox，HTTP请求和Matching Worker都不
        直接访问Redis。独立发布Worker稍后将其至少一次写入stream:orders。
        """

        # TRADE_CREATED提供完整成交快照，未来可供推送、审计等消费者使用。
        trade_event_id = self.event_id_factory()
        self.outbox_repository.create_event(
            db=db,
            event_id=trade_event_id,
            aggregate_type="TRADE",
            aggregate_id=trade.trade_id,
            event_type="TRADE_CREATED",
            payload={
                "event_id": trade_event_id,
                "event_type": "TRADE_CREATED",
                "trade_id": trade.trade_id,
                "order_id": trade.order_id,
                "account_id": trade.account_id,
                "market_event_id": trade.market_event_id,
                "market_stream_message_id": trade.market_stream_message_id,
                "exchange_id": trade.exchange_id,
                "symbol": trade.symbol,
                "order_book_id": trade.order_book_id,
                "trading_day": trade.trading_day.isoformat(),
                "direction": trade.direction,
                "offset_flag": trade.offset_flag,
                "order_type": order.order_type,
                "resolved_price": _decimal_string(
                    getattr(
                        order,
                        "resolved_price",
                        getattr(order, "limit_price", Decimal("0")),
                    )
                ),
                "market_protection_price": (
                    _decimal_string(order.market_protection_price)
                    if getattr(order, "market_protection_price", None) is not None
                    else None
                ),
                "trade_price": _decimal_string(trade.trade_price),
                "trade_volume": trade.trade_volume,
                "turnover": _decimal_string(trade.turnover),
                "margin": _decimal_string(trade.margin),
                "premium_cash_flow": _decimal_string(
                    getattr(trade, "premium_cash_flow", None) or Decimal("0")
                ),
                "commission": _decimal_string(trade.commission),
                "realized_pnl": _decimal_string(trade.realized_pnl),
                "daily_close_pnl": _decimal_string(
                    trade.daily_close_pnl
                ),
                "trade_time": trade.trade_time.isoformat(),
                "created_at": trade.created_at.isoformat(),
            },
            created_at=now,
        )

        # 订单状态事件由现有订单Consumer消费，用数据库最新状态更新或删除
        # Redis活动订单索引。结算服务自身禁止直接碰活动索引。
        status_event_type = (
            "ORDER_FILLED"
            if order.status == OrderStatus.FILLED.value
            else "ORDER_PARTIALLY_FILLED"
        )
        order_event_id = self.event_id_factory()
        self.outbox_repository.create_event(
            db=db,
            event_id=order_event_id,
            aggregate_type="ORDER",
            aggregate_id=order.order_id,
            event_type=status_event_type,
            payload={
                "event_id": order_event_id,
                "event_type": status_event_type,
                "order_id": order.order_id,
                "account_id": order.account_id,
                "exchange_id": order.exchange_id,
                "symbol": order.symbol,
                "order_book_id": order.order_book_id,
                "direction": order.direction,
                "offset_flag": order.offset_flag,
                "order_type": order.order_type,
                "resolved_price": _decimal_string(
                    getattr(
                        order,
                        "resolved_price",
                        getattr(order, "limit_price", Decimal("0")),
                    )
                ),
                "market_protection_price": (
                    _decimal_string(order.market_protection_price)
                    if getattr(order, "market_protection_price", None) is not None
                    else None
                ),
                "status": order.status,
                "traded_volume": order.traded_volume,
                "remaining_volume": order.remaining_volume,
                "cancelled_volume": order.cancelled_volume,
                "average_price": _decimal_string(order.average_price),
                "frozen_margin": _decimal_string(order.frozen_margin),
                "frozen_cash": _decimal_string(
                    getattr(order, "frozen_cash", None) or Decimal("0")
                ),
                "frozen_commission": _decimal_string(order.frozen_commission),
                "frozen_position_volume": order.frozen_position_volume,
                "released_margin": _decimal_string(trade.margin),
                "commission": _decimal_string(trade.commission),
                "realized_pnl": _decimal_string(trade.realized_pnl),
                "daily_close_pnl": _decimal_string(
                    trade.daily_close_pnl
                ),
                "updated_at": order.updated_at.isoformat(),
            },
            created_at=now,
        )

        # 成交事务已经计算出PostgreSQL最终账户和持仓事实；把完整绝对值
        # 同事务写入Outbox，客户端无需收到Trade后重新加载整份快照。
        self.realtime_fact_events.create_position_updated(
            db,
            position=position,
            occurred_at=now,
        )
        self.realtime_fact_events.create_account_updated(
            db,
            account=account,
            occurred_at=now,
            account_id=order.account_id,
        )

    def _get_affected_position(self, db: Session, *, order) -> Position:
        """按订单开平方向取得刚被当前事务修改的持仓汇总。"""

        # 生产Session关闭autoflush；期权策略刚创建的新持仓必须先flush，
        # 后续同事务SELECT才能取得数据库生成及已写入的真实绝对状态。
        db.flush()
        if order.offset_flag == OffsetFlag.OPEN.value:
            direction = (
                PositionDirection.LONG.value
                if order.direction == OrderDirection.BUY.value
                else PositionDirection.SHORT.value
            )
        else:
            direction = (
                PositionDirection.LONG.value
                if order.direction == OrderDirection.SELL.value
                else PositionDirection.SHORT.value
            )
        position = self.position_repository.get_for_update(
            db,
            account_id=order.account_id,
            exchange_id=order.exchange_id,
            symbol=order.symbol,
            direction=direction,
        )
        if position is None:
            raise DataAccessError(
                "成交事务完成后找不到受影响持仓",
                error_code="SETTLEMENT_POSITION_NOT_FOUND",
            )
        return position

    def settle(
        self,
        db: Session,
        command: SettlementCommand,
    ) -> SettlementResult:
        """
        执行单个订单的一次成交事务并在成功后提交。

        调用方应当为每个候选订单提供独立Session，使一笔订单失败时其他订单
        仍可先提交。若任一订单发生临时错误，外层会保留整条Tick为Pending；
        重试时已成功订单依靠order_id+market_event_id幂等跳过。
        """

        # 纯计算结果和业务追踪上下文明确分离；结算只从 command 获取
        # 订单、行情及 Redis 标识，从 match_result 获取拟成交价格和数量。
        match_result = command.match_result

        # 纯撮合明确返回不成交时，无需开启数据库写事务。
        if not match_result.matched:
            return SettlementResult(None, command.order_id, "NOT_MATCHED")

        try:
            # 在取得订单行锁前先进入结算共享锁域。排他日终会等待所有已开始
            # 成交事务提交，并阻止新的成交跨越持久化屏障。
            self.settlement_gate_service.ensure_trading_open(db)
            # 固定先锁订单，再锁账户、持仓，降低并发结算时的死锁概率。
            order = self.order_repository.get_by_order_id_for_update(
                db, command.order_id
            )
            if order is None:
                db.rollback()
                return SettlementResult(None, command.order_id, "ORDER_NOT_FOUND")

            self.settlement_gate_service.ensure_trading_open(
                db, trading_day=getattr(order, "trading_day", None)
            )

            existing = self.trade_repository.get_by_order_market_event(
                db,
                order_id=command.order_id,
                market_event_id=command.market_event_id,
            )
            if existing is not None:
                # 成交事务可能已经提交，但Worker尚未来得及XACK便崩溃。
                # 该分支返回原成交编号，禁止重复更新订单、账户和持仓。
                trade_id = existing.trade_id
                db.rollback()
                return SettlementResult(trade_id, command.order_id, "IDEMPOTENT")

            if not self._is_matchable_order(order, match_result):
                # Redis活动索引允许短暂滞后，数据库确认失效即可安全跳过。
                db.rollback()
                return SettlementResult(None, command.order_id, "ORDER_INACTIVE")

            # 引擎使用的是 Redis 候选快照，拿到数据库行锁后必须以数据库
            # 当前剩余量为上限，避免并发 Tick 把订单成交为负数。
            fill_volume = min(match_result.fill_volume, order.remaining_volume)
            remaining_before = order.remaining_volume
            traded_before = order.traded_volume
            average_before = order.average_price or Decimal("0")

            account = self.account_repository.get_by_account_id_for_update(
                db, order.account_id
            )
            if account is None:
                raise DataAccessError(
                    "成交订单对应账户不存在",
                    error_code="SETTLEMENT_ACCOUNT_NOT_FOUND",
                )
            instrument = self.instrument_repository.get_by_order_book_id(
                db, order.order_book_id
            )
            if instrument is None:
                raise DataAccessError(
                    "成交订单对应合约不存在",
                    error_code="SETTLEMENT_INSTRUMENT_NOT_FOUND",
                )
            if instrument.instrument_type != order.instrument_type:
                raise DataAccessError(
                    "订单产品类型与合约事实不一致",
                    error_code="SETTLEMENT_PRODUCT_TYPE_MISMATCH",
                )
            product_strategy = self.product_registry.resolve(
                order.instrument_type
            )

            fill_price = quantize_money(match_result.fill_price)
            now = utc_now()
            if order.offset_flag != OffsetFlag.OPEN.value:
                trade = self.close_handler.apply(
                    db,
                    order=order,
                    account=account,
                    instrument=instrument,
                    command=command,
                    fill_volume=fill_volume,
                    fill_price=fill_price,
                    remaining_before=remaining_before,
                    traded_before=traded_before,
                    average_before=average_before,
                    trade_id=self.trade_id_factory(),
                    now=now,
                )
                position = self._get_affected_position(db, order=order)
                self._create_outbox_events(
                    db,
                    trade=trade,
                    order=order,
                    account=account,
                    position=position,
                    now=now,
                )
                db.commit()
                return SettlementResult(
                    trade.trade_id,
                    order.order_id,
                    "SETTLED",
                )

            # 商品期权卖出开仓必须在Order→Account行锁内按最新期权及标的
            # 行情执行最终保证金校验。500ms提前重估只能降低补冻概率，不能
            # 替代成交事务的最后一道保护。
            margin_check = self.option_order_margin_service.ensure_locked(
                db,
                order=order,
                account=account,
                instrument=instrument,
                final_check=True,
            )
            if margin_check.action in {
                "RISK_BLOCKED",
                "MARGIN_DEFICIT",
                "VALUATION_UNAVAILABLE",
            }:
                # 风险状态变更需要可靠提交，但本轮不得创建Trade、修改订单
                # 成交数量或转移冻结资源。后续用户仍可撤销订单或执行平仓。
                db.commit()
                return SettlementResult(
                    None,
                    order.order_id,
                    margin_check.action,
                )

            position_direction = (
                PositionDirection.LONG.value
                if order.direction == OrderDirection.BUY.value
                else PositionDirection.SHORT.value
            )
            position = self.position_repository.get_for_update(
                db,
                account_id=order.account_id,
                exchange_id=order.exchange_id,
                symbol=order.symbol,
                direction=position_direction,
            )

            if product_strategy.family == ProductFamily.OPTIONS:
                # 期权权利金和卖方保证金的现金语义与期货不同，必须走
                # 独立策略；数据库锁顺序仍保持 Order→Account→Position。
                trade = self.option_strategy.apply_open(
                    db=db,
                    order=order,
                    account=account,
                    instrument=instrument,
                    command=command,
                    fill_volume=fill_volume,
                    fill_price=fill_price,
                    remaining_before=remaining_before,
                    traded_before=traded_before,
                    average_before=average_before,
                    position=position,
                    trade_id=self.trade_id_factory(),
                    position_id=self.position_id_factory(),
                    position_detail_id=self.position_detail_id_factory(),
                    now=now,
                    fee_calculator=self.fee_calculator,
                    trade_repository=self.trade_repository,
                    position_repository=self.position_repository,
                )
                position = self._get_affected_position(db, order=order)
                self._create_outbox_events(
                    db,
                    trade=trade,
                    order=order,
                    account=account,
                    position=position,
                    now=now,
                )
                db.commit()
                return SettlementResult(
                    trade.trade_id,
                    order.order_id,
                    "SETTLED",
                )

            if product_strategy.family != ProductFamily.FUTURES:
                raise DataAccessError(
                    "成交产品结算策略尚未实现",
                    error_code="PRODUCT_TRADE_SETTLEMENT_NOT_IMPLEMENTED",
                )

            # 保证金仍从成交前剩余冻结资源按数量转为实际占用；手续费则
            # 明确区分“本次释放的预计冻结值”和“按实际成交价重算的值”。
            allocated_margin = self._allocate_frozen(
                order.frozen_margin,
                fill_volume=fill_volume,
                remaining_volume_before_fill=remaining_before,
            )
            released_frozen_commission = self._allocate_frozen(
                order.frozen_commission,
                fill_volume=fill_volume,
                remaining_volume_before_fill=remaining_before,
            )
            actual_commission = self.fee_calculator.calculate_from_snapshot(
                price=fill_price,
                volume=fill_volume,
                commission_type=order.commission_type,
                commission_parameter=order.commission_parameter,
                contract_multiplier=order.commission_contract_multiplier,
            )
            # 成交价格和成交额在入库前统一量化到Numeric(24,6)精度。
            turnover = quantize_money(
                fill_price
                * Decimal(fill_volume)
                * Decimal(order.commission_contract_multiplier)
            )
            # Trade.commission 只记录实际手续费，不能写入预计冻结手续费。
            trade = Trade(
                trade_id=self.trade_id_factory(),
                order_id=order.order_id,
                account_id=order.account_id,
                market_event_id=command.market_event_id,
                market_stream_message_id=command.market_stream_message_id,
                order_book_id=order.order_book_id,
                exchange_id=order.exchange_id,
                symbol=order.symbol,
                trading_day=order.trading_day,
                direction=order.direction,
                offset_flag=order.offset_flag,
                trade_price=fill_price,
                trade_volume=fill_volume,
                turnover=turnover,
                margin=allocated_margin,
                commission=actual_commission,
                realized_pnl=Decimal("0.000000"),
                daily_close_pnl=Decimal("0.000000"),
                trade_time=command.tick_event_time,
                created_at=now,
            )
            self.trade_repository.add(db, trade)

            # 更新订单数量及加权平均成交价，并继续维持数量恒等式：
        # 委托数量守恒：总量 = 已成交 + 剩余 + 已撤销。
            new_traded = traded_before + fill_volume
            new_remaining = remaining_before - fill_volume
            order.traded_volume = new_traded
            order.remaining_volume = new_remaining
            order.average_price = quantize_money(
                (
                    average_before * Decimal(traded_before)
                    + fill_price * Decimal(fill_volume)
                )
                / Decimal(new_traded)
            )
            order.status = (
                OrderStatus.FILLED.value
                if new_remaining == 0
                else OrderStatus.PARTIALLY_FILLED.value
            )
            if new_remaining == 0:
                order.margin_risk_state = AccountRiskState.NORMAL.value
            order.frozen_margin = quantize_money(
                order.frozen_margin - allocated_margin
            )
            order.frozen_commission = quantize_money(
                order.frozen_commission - released_frozen_commission
            )
            order.updated_at = now

            if (
                account.frozen_margin < allocated_margin
                or account.frozen_commission
                < released_frozen_commission
            ):
                raise DataAccessError(
                    "开仓成交账户冻结资源不一致",
                    error_code="OPEN_RESOURCE_INCONSISTENT",
                )

            # 保证金只从冻结转为实际占用；手续费先释放预计冻结额，再按
            # 实际成交价扣除，二者差额才会改变 available_cash。
            account.frozen_margin = quantize_money(
                account.frozen_margin - allocated_margin
            )
            account.used_margin = quantize_money(
                account.used_margin + allocated_margin
            )
            account.frozen_commission = quantize_money(
                account.frozen_commission - released_frozen_commission
            )
            account.used_commission = quantize_money(
                account.used_commission + actual_commission
            )
            account.daily_commission = quantize_money(
                account.daily_commission + actual_commission
            )
            account.cash_balance = quantize_money(
                account.cash_balance - actual_commission
            )
            # 下单时已扣除预计手续费。成交时释放预计值并扣除实际值，
            # BY_AMOUNT 改善或劣化成交因此会正确反映到可用资金。
            account.available_cash = quantize_money(
                account.available_cash
                + released_frozen_commission
                - actual_commission
            )
            account.equity = quantize_money(
                account.cash_balance + account.unrealized_pnl
            )
            account.daily_pnl = quantize_money(
                account.daily_position_pnl
                + account.daily_close_pnl
                - account.daily_commission
            )
            account.updated_at = now

            if position is None:
                # 首次开仓成交时创建对应方向的持仓汇总。由于已经先锁定账户，
                # 同账户并发事务不会同时创建两条相同方向持仓。
                position = Position(
                    position_id=self.position_id_factory(),
                    account_id=order.account_id,
                    order_book_id=order.order_book_id,
                    exchange_id=order.exchange_id,
                    symbol=order.symbol,
                    direction=position_direction,
                    total_volume=0,
                    today_volume=0,
                    yesterday_volume=0,
                    frozen_volume=0,
                    settlement_locked_volume=0,
                    available_volume=0,
                    average_open_price=Decimal("0.000000"),
                    position_cost=Decimal("0.000000"),
                    used_margin=Decimal("0.000000"),
                    initial_occupied_margin=Decimal("0.000000"),
                    realtime_required_margin=Decimal("0.000000"),
                    option_market_value=Decimal("0.000000"),
                    multiplier_snapshot=Decimal(
                        order.commission_contract_multiplier
                    ),
                    realized_pnl=Decimal("0.000000"),
                    unrealized_pnl=Decimal("0.000000"),
                    daily_position_pnl=Decimal("0.000000"),
                    daily_close_pnl=Decimal("0.000000"),
                    trading_day=order.trading_day,
                    created_at=now,
                    updated_at=now,
                )
                self.position_repository.add(db, position)

            # 同方向继续加仓时，按成交数量重新计算持仓平均开仓价格。
            old_position_volume = position.total_volume
            new_position_volume = old_position_volume + fill_volume
            position.average_open_price = quantize_money(
                (
                    position.average_open_price * Decimal(old_position_volume)
                    + fill_price * Decimal(fill_volume)
                )
                / Decimal(new_position_volume)
            )
            position.total_volume = new_position_volume
            position.today_volume += fill_volume
            position.available_volume = (
                position.total_volume
                - position.frozen_volume
                - position.settlement_locked_volume
            )
            position.position_cost = quantize_money(
                position.position_cost + turnover
            )
            # 第一版沿用下单价冻结的保证金，不按实际成交价二次重算，
            # 从而确保资金守恒且 SELL 改善成交不会引入追加资金检查。
            position.used_margin = quantize_money(
                position.used_margin + allocated_margin
            )
            position.initial_occupied_margin = quantize_money(
                position.initial_occupied_margin + allocated_margin
            )
            if Decimal(position.multiplier_snapshot) != Decimal(
                order.commission_contract_multiplier
            ):
                raise DataAccessError(
                    "期货订单与既有持仓乘数快照不一致",
                    error_code="POSITION_MULTIPLIER_INCONSISTENT",
                )
            position.trading_day = order.trading_day
            position.updated_at = now

            # 每一条开仓Trade对应一条独立明细，为后续平今、平昨保留依据。
            detail = PositionDetail(
                position_detail_id=self.position_detail_id_factory(),
                position_id=position.position_id,
                account_id=order.account_id,
                open_trade_id=trade.trade_id,
                order_book_id=order.order_book_id,
                exchange_id=order.exchange_id,
                symbol=order.symbol,
                direction=position_direction,
                open_trading_day=order.trading_day,
                open_price=fill_price,
                pnl_base_price=fill_price,
                original_volume=fill_volume,
                remaining_volume=fill_volume,
                frozen_volume=0,
                open_margin=allocated_margin,
                remaining_margin=allocated_margin,
                initial_occupied_margin=allocated_margin,
                realtime_required_margin=Decimal("0.000000"),
                multiplier_snapshot=Decimal(
                    order.commission_contract_multiplier
                ),
                open_commission=actual_commission,
                status=PositionDetailStatus.OPEN.value,
                created_at=now,
                updated_at=now,
            )
            self.position_repository.add_detail(db, detail)
            # Outbox与全部业务变更一起提交，Redis暂时不可用也不会丢失事件。
            self._create_outbox_events(
                db,
                trade=trade,
                order=order,
                account=account,
                position=position,
                now=now,
            )
            db.commit()
            return SettlementResult(trade.trade_id, order.order_id, "SETTLED")

        except IntegrityError as exc:
            # 极端并发下数据库唯一约束可能先于显式幂等查询命中。
            db.rollback()
            existing = self.trade_repository.get_by_order_market_event(
                db,
                order_id=command.order_id,
                market_event_id=command.market_event_id,
            )
            if existing is not None:
                return SettlementResult(
                    existing.trade_id, command.order_id, "IDEMPOTENT"
                )
            raise DataAccessError(
                "成交结算唯一约束冲突",
                error_code="TRADE_SETTLEMENT_CONFLICT",
            ) from exc
        except DataAccessError:
            db.rollback()
            raise
        except SQLAlchemyError as exc:
            db.rollback()
            raise DataAccessError(
                "成交结算数据库操作失败",
                error_code="TRADE_SETTLEMENT_FAILED",
            ) from exc
        except Exception:
            db.rollback()
            raise


class TradeQueryService:
    """
    成交只读查询服务。

    API层通过本服务读取成交，避免直接依赖Repository。当前不管理事务，
    也不会修改成交、订单、账户或Redis状态。
    """

    def __init__(
        self,
        repository: TradeRepository | None = None,
        position_allocation_repository: (
            TradePositionAllocationRepository | None
        ) = None,
    ):
        self.repository = repository or TradeRepository()
        self.position_allocation_repository = (
            position_allocation_repository
            or TradePositionAllocationRepository()
        )

    def get(self, db: Session, trade_id: str) -> Trade:
        trade = self.repository.get_by_trade_id(db, trade_id.strip())
        if trade is None:
            raise ResourceNotFoundError("成交不存在", error_code="TRADE_NOT_FOUND")
        return trade

    def list(
        self,
        db: Session,
        *,
        account_id: str | None = None,
        order_id: str | None = None,
        after_id: int | None = None,
        limit: int = 100,
    ) -> Sequence[Trade]:
        return self.repository.list(
            db,
            account_id=account_id.strip() if account_id else None,
            order_id=order_id.strip() if order_id else None,
            after_id=after_id,
            limit=limit,
        )

    def list_page(
        self,
        db: Session,
        *,
        account_id: str | None = None,
        order_id: str | None = None,
        cursor: str | None,
        limit: int,
    ) -> TradePageResponse:
        """返回成交倒序页和绑定当前过滤条件的不透明下一页游标。"""

        normalized_account_id = (
            account_id.strip() if account_id else None
        )
        normalized_order_id = order_id.strip() if order_id else None
        filters = {
            "account_id": normalized_account_id or "",
            "order_id": normalized_order_id or "",
        }
        before_id = None
        if cursor is not None:
            before_id = decode_cursor(
                cursor,
                expected_kind="trades",
                expected_filters=filters,
            ).before_id
        rows = list(
            self.repository.list_page(
                db,
                account_id=normalized_account_id,
                order_id=normalized_order_id,
                before_id=before_id,
                fetch_size=limit + 1,
            )
        )
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = (
            encode_cursor(
                kind="trades",
                before_id=items[-1].id,
                filters=filters,
            )
            if has_more and items
            else None
        )
        return TradePageResponse(
            items=items,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def list_position_allocations(
        self,
        db: Session,
        trade_id: str,
        *,
        trade=None,
    ):
        """查询一笔平仓成交具体消费的 PositionDetail 明细。"""

        normalized_trade_id = trade_id.strip()
        trade = trade or self.repository.get_by_trade_id(
            db, normalized_trade_id
        )
        if trade is None:
            raise ResourceNotFoundError(
                "成交不存在",
                error_code="TRADE_NOT_FOUND",
            )
        return self.position_allocation_repository.list_by_trade(
            db,
            normalized_trade_id,
        )


class PositionQueryService:
    """持仓只读查询服务，只返回PostgreSQL中已提交的持仓汇总。"""

    def __init__(self, repository: PositionRepository | None = None):
        self.repository = repository or PositionRepository()

    def list(self, db: Session, account_id: str) -> Sequence[Position]:
        return self.repository.list_by_account(db, account_id.strip())
