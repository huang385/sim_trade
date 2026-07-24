from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence
from uuid import uuid4

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.common.decimal_utils import quantize_money
from app.common.exceptions import DataAccessError, ResourceNotFoundError
from app.common.time_utils import utc_now
from app.enums.order_enums import (
    OffsetFlag,
    OrderDirection,
    OrderStatus,
    OrderType,
    PositionDetailStatus,
    PositionDirection,
)
from app.matching.models import MatchResult
from app.models.position import Position
from app.models.position_detail import PositionDetail
from app.models.trade import Trade
from app.repositories.account_repository import AccountRepository
from app.repositories.instrument_repository import InstrumentRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.outbox_repository import OutboxRepository
from app.repositories.position_repository import PositionRepository
from app.repositories.trade_repository import TradeRepository


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
        outbox_repository: OutboxRepository | None = None,
        trade_id_factory: Callable[[], str] | None = None,
        position_id_factory: Callable[[], str] | None = None,
        position_detail_id_factory: Callable[[], str] | None = None,
        event_id_factory: Callable[[], str] | None = None,
    ):
        # 所有依赖都允许从构造函数注入，便于单元测试精确模拟某一步失败，
        # 同时生产环境默认使用真实Repository和UUID编号工厂。
        self.order_repository = order_repository or OrderRepository()
        self.account_repository = account_repository or AccountRepository()
        self.instrument_repository = instrument_repository or InstrumentRepository()
        self.trade_repository = trade_repository or TradeRepository()
        self.position_repository = position_repository or PositionRepository()
        self.outbox_repository = outbox_repository or OutboxRepository()
        self.trade_id_factory = trade_id_factory or (lambda: _generate_id("T"))
        self.position_id_factory = position_id_factory or (lambda: _generate_id("P"))
        self.position_detail_id_factory = position_detail_id_factory or (
            lambda: _generate_id("PD")
        )
        self.event_id_factory = event_id_factory or (
            lambda: f"EVT-{uuid4().hex.upper()}"
        )

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
            and order.order_type == OrderType.LIMIT.value
            and order.offset_flag == OffsetFlag.OPEN.value
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
                "trade_price": _decimal_string(trade.trade_price),
                "trade_volume": trade.trade_volume,
                "turnover": _decimal_string(trade.turnover),
                "margin": _decimal_string(trade.margin),
                "commission": _decimal_string(trade.commission),
                "realized_pnl": _decimal_string(trade.realized_pnl),
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
                "status": order.status,
                "traded_volume": order.traded_volume,
                "remaining_volume": order.remaining_volume,
                "cancelled_volume": order.cancelled_volume,
                "average_price": _decimal_string(order.average_price),
                "frozen_margin": _decimal_string(order.frozen_margin),
                "frozen_commission": _decimal_string(order.frozen_commission),
                "updated_at": order.updated_at.isoformat(),
            },
            created_at=now,
        )

    def settle(self, db: Session, result: MatchResult) -> SettlementResult:
        """
        执行单个订单的一次成交事务并在成功后提交。

        调用方应当为每个候选订单提供独立Session，使一笔订单失败时其他订单
        仍可先提交。若任一订单发生临时错误，外层会保留整条Tick为Pending；
        重试时已成功订单依靠order_id+market_event_id幂等跳过。
        """

        # 纯撮合明确返回不成交时，无需开启数据库写事务。
        if not result.matched:
            return SettlementResult(None, result.order_id, "NOT_MATCHED")

        try:
            # 固定先锁订单，再锁账户、持仓，降低并发结算时的死锁概率。
            order = self.order_repository.get_by_order_id_for_update(
                db, result.order_id
            )
            if order is None:
                db.rollback()
                return SettlementResult(None, result.order_id, "ORDER_NOT_FOUND")

            existing = self.trade_repository.get_by_order_market_event(
                db,
                order_id=result.order_id,
                market_event_id=result.market_event_id,
            )
            if existing is not None:
                # 成交事务可能已经提交，但Worker尚未来得及XACK便崩溃。
                # 该分支返回原成交编号，禁止重复更新订单、账户和持仓。
                trade_id = existing.trade_id
                db.rollback()
                return SettlementResult(trade_id, result.order_id, "IDEMPOTENT")

            if not self._is_matchable_order(order, result):
                # Redis活动索引允许短暂滞后，数据库确认失效即可安全跳过。
                db.rollback()
                return SettlementResult(None, result.order_id, "ORDER_INACTIVE")

            # 引擎使用的是 Redis 候选快照，拿到数据库行锁后必须以数据库
            # 当前剩余量为上限，避免并发 Tick 把订单成交为负数。
            fill_volume = min(result.fill_volume, order.remaining_volume)
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

            # 保证金和手续费都从“成交前订单剩余冻结资源”按剩余量分配。
            # 最后一笔会直接拿走全部剩余值，防止六位小数舍入残留尾差。
            allocated_margin = self._allocate_frozen(
                order.frozen_margin,
                fill_volume=fill_volume,
                remaining_volume_before_fill=remaining_before,
            )
            allocated_commission = self._allocate_frozen(
                order.frozen_commission,
                fill_volume=fill_volume,
                remaining_volume_before_fill=remaining_before,
            )
            # 成交价格和成交额在入库前统一量化到Numeric(24,6)精度。
            fill_price = quantize_money(result.fill_price)
            turnover = quantize_money(
                fill_price
                * Decimal(fill_volume)
                * Decimal(instrument.contract_multiplier)
            )
            now = utc_now()
            # Trade记录本次资源转换结果，不重新按成交价计算保证金和手续费。
            trade = Trade(
                trade_id=self.trade_id_factory(),
                order_id=order.order_id,
                account_id=order.account_id,
                market_event_id=result.market_event_id,
                market_stream_message_id=result.market_stream_message_id,
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
                commission=allocated_commission,
                realized_pnl=Decimal("0.000000"),
                trade_time=result.tick_event_time,
                created_at=now,
            )
            self.trade_repository.add(db, trade)

            # 更新订单数量及加权平均成交价，并继续维持数量恒等式：
            # total = traded + remaining + cancelled。
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
            order.frozen_margin = quantize_money(
                order.frozen_margin - allocated_margin
            )
            order.frozen_commission = quantize_money(
                order.frozen_commission - allocated_commission
            )
            order.updated_at = now

            # 下单时 available_cash 已经一次性减少。本处只把冻结资源转成
            # 实际占用，并扣除手续费，绝不能再次减少 available_cash。
            account.frozen_margin = quantize_money(
                account.frozen_margin - allocated_margin
            )
            account.used_margin = quantize_money(
                account.used_margin + allocated_margin
            )
            account.frozen_commission = quantize_money(
                account.frozen_commission - allocated_commission
            )
            account.used_commission = quantize_money(
                account.used_commission + allocated_commission
            )
            account.cash_balance = quantize_money(
                account.cash_balance - allocated_commission
            )
            account.equity = quantize_money(
                account.equity - allocated_commission
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
                    available_volume=0,
                    average_open_price=Decimal("0.000000"),
                    position_cost=Decimal("0.000000"),
                    used_margin=Decimal("0.000000"),
                    realized_pnl=Decimal("0.000000"),
                    unrealized_pnl=Decimal("0.000000"),
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
            position.available_volume += fill_volume
            position.position_cost = quantize_money(
                position.position_cost + turnover
            )
            # 第一版沿用下单价冻结的保证金，不按实际成交价二次重算，
            # 从而确保资金守恒且 SELL 改善成交不会引入追加资金检查。
            position.used_margin = quantize_money(
                position.used_margin + allocated_margin
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
                original_volume=fill_volume,
                remaining_volume=fill_volume,
                frozen_volume=0,
                open_margin=allocated_margin,
                open_commission=allocated_commission,
                status=PositionDetailStatus.OPEN.value,
                created_at=now,
                updated_at=now,
            )
            self.position_repository.add_detail(db, detail)
            # Outbox与全部业务变更一起提交，Redis暂时不可用也不会丢失事件。
            self._create_outbox_events(db, trade=trade, order=order, now=now)
            db.commit()
            return SettlementResult(trade.trade_id, order.order_id, "SETTLED")

        except IntegrityError as exc:
            # 极端并发下数据库唯一约束可能先于显式幂等查询命中。
            db.rollback()
            existing = self.trade_repository.get_by_order_market_event(
                db,
                order_id=result.order_id,
                market_event_id=result.market_event_id,
            )
            if existing is not None:
                return SettlementResult(
                    existing.trade_id, result.order_id, "IDEMPOTENT"
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

    def __init__(self, repository: TradeRepository | None = None):
        self.repository = repository or TradeRepository()

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
    ) -> Sequence[Trade]:
        return self.repository.list(
            db,
            account_id=account_id.strip() if account_id else None,
            order_id=order_id.strip() if order_id else None,
        )


class PositionQueryService:
    """持仓只读查询服务，只返回PostgreSQL中已提交的持仓汇总。"""

    def __init__(self, repository: PositionRepository | None = None):
        self.repository = repository or PositionRepository()

    def list(self, db: Session, account_id: str) -> Sequence[Position]:
        return self.repository.list_by_account(db, account_id.strip())
