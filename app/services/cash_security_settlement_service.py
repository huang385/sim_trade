"""现金证券成交的原子结算，不调用衍生品结算、保证金或平仓分配服务。"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.common.decimal_utils import quantize_money
from app.common.exceptions import BusinessRuleError, DataAccessError
from app.common.time_utils import utc_now
from app.enums.reference_data_enums import CommissionType
from app.enums.order_enums import OrderDirection, OrderStatus
from app.models.cash_security_order_fee_accumulator import CashSecurityOrderFeeAccumulator
from app.models.cash_security_trade_fee_component import CashSecurityTradeFeeComponent
from app.models.position import Position
from app.models.trade import Trade
from app.repositories.account_repository import AccountRepository
from app.repositories.cash_security_fee_repository import CashSecurityFeeRepository
from app.repositories.instrument_repository import InstrumentRepository
from app.repositories.order_fee_component_snapshot_repository import OrderFeeComponentSnapshotRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.outbox_repository import OutboxRepository
from app.repositories.position_repository import PositionRepository
from app.repositories.trade_repository import TradeRepository
from app.matching.cash_security import CashSecurityMatchResult
from app.services.cash_security_position_service import CashSecurityPositionService
from app.services.realtime_fact_event_service import RealtimeFactEventService


CASH_SECURITY_TYPES = frozenset({"STOCK", "CONVERTIBLE_BOND"})
ACTIVE_STATUSES = frozenset({OrderStatus.ACCEPTED.value, OrderStatus.PARTIALLY_FILLED.value})


@dataclass(frozen=True)
class CashSecuritySettlementResult:
    trade_id: str | None
    order_id: str
    action: str


class CashSecuritySettlementService:
    """按 Order → Account → Position → 费用累计行的固定顺序完成一笔成交。"""

    def __init__(
        self,
        *,
        order_repository: OrderRepository | None = None,
        account_repository: AccountRepository | None = None,
        instrument_repository: InstrumentRepository | None = None,
        position_repository: PositionRepository | None = None,
        trade_repository: TradeRepository | None = None,
        snapshot_repository: OrderFeeComponentSnapshotRepository | None = None,
        fee_repository: CashSecurityFeeRepository | None = None,
        outbox_repository: OutboxRepository | None = None,
    ) -> None:
        self.order_repository = order_repository or OrderRepository()
        self.account_repository = account_repository or AccountRepository()
        self.instrument_repository = instrument_repository or InstrumentRepository()
        self.position_repository = position_repository or PositionRepository()
        self.trade_repository = trade_repository or TradeRepository()
        self.snapshot_repository = snapshot_repository or OrderFeeComponentSnapshotRepository()
        self.fee_repository = fee_repository or CashSecurityFeeRepository()
        self.outbox_repository = outbox_repository or OutboxRepository()
        self.realtime_events = RealtimeFactEventService(repository=self.outbox_repository)

    @staticmethod
    def _id(prefix: str) -> str:
        return f"{prefix}-{uuid4().hex.upper()}"

    @staticmethod
    def _cash_account(account) -> None:
        if account is None or account.account_type not in {"STOCK", "SECURITIES_CASH"}:
            raise DataAccessError("现金证券账户事实不一致", error_code="CASH_SECURITY_ACCOUNT_REQUIRED")

    @staticmethod
    def _fee_from_snapshot(snapshot, *, price: Decimal, volume: int, turnover: Decimal) -> Decimal:
        try:
            kind = CommissionType(snapshot.calculation_type)
        except ValueError as exc:
            raise DataAccessError("现金证券费用快照无效", error_code="CASH_SECURITY_FEE_SNAPSHOT_INVALID") from exc
        raw = (
            Decimal(volume) * Decimal(snapshot.commission_parameter)
            if kind == CommissionType.BY_VOLUME
            else turnover * Decimal(snapshot.commission_parameter)
        )
        return max(quantize_money(raw), quantize_money(Decimal(snapshot.minimum_fee)))

    def _settle_fees(self, db: Session, *, order, trade_id: str, price: Decimal, volume: int, now: datetime) -> Decimal:
        snapshots = self.snapshot_repository.list_by_order_ids(db, [order.order_id]).get(order.order_id, [])
        if not snapshots:
            raise DataAccessError("现金证券订单缺少费用快照", error_code="CASH_SECURITY_FEE_SNAPSHOT_MISSING")
        turnover = quantize_money(price * Decimal(volume))
        total = Decimal("0")
        rows: list[CashSecurityTradeFeeComponent] = []
        for snapshot in snapshots:
            if snapshot.aggregation_scope == "TRADE":
                fee = self._fee_from_snapshot(snapshot, price=price, volume=volume, turnover=turnover)
            elif snapshot.aggregation_scope == "ORDER":
                accumulator = self.fee_repository.get_accumulator_for_update(db, order_id=order.order_id, fee_type=snapshot.fee_type)
                if accumulator is None:
                    accumulator = CashSecurityOrderFeeAccumulator(order_id=order.order_id, fee_type=snapshot.fee_type, cumulative_volume=0, cumulative_turnover=Decimal("0"), charged_fee=Decimal("0"), created_at=now, updated_at=now)
                    self.fee_repository.add_accumulator(db, accumulator)
                    db.flush()
                next_volume = accumulator.cumulative_volume + volume
                next_turnover = quantize_money(accumulator.cumulative_turnover + turnover)
                if CommissionType(snapshot.calculation_type) == CommissionType.BY_VOLUME:
                    due = Decimal(next_volume) * Decimal(snapshot.commission_parameter)
                else:
                    due = next_turnover * Decimal(snapshot.commission_parameter)
                total_due = max(quantize_money(due), quantize_money(Decimal(snapshot.minimum_fee)))
                fee = quantize_money(total_due - accumulator.charged_fee)
                accumulator.cumulative_volume = next_volume
                accumulator.cumulative_turnover = next_turnover
                accumulator.charged_fee = total_due
                accumulator.updated_at = now
            else:
                raise DataAccessError("现金证券费用聚合范围无效", error_code="CASH_SECURITY_FEE_SCOPE_INVALID")
            total += fee
            rows.append(CashSecurityTradeFeeComponent(trade_id=trade_id, fee_type=snapshot.fee_type, fee_amount=fee, created_at=now))
        self.fee_repository.add_trade_components(db, rows)
        return quantize_money(total)

    def _position_for_update(self, db: Session, *, order, instrument, now: datetime) -> Position:
        position = self.position_repository.get_for_update(db, account_id=order.account_id, exchange_id=order.exchange_id, symbol=order.symbol, direction="LONG")
        if position is not None:
            return position
        position = Position(
            position_id=self._id("CSP"), account_id=order.account_id,
            order_book_id=order.order_book_id, exchange_id=order.exchange_id,
            symbol=order.symbol, instrument_type=order.instrument_type, direction="LONG",
            total_volume=0, today_volume=0, yesterday_volume=0, frozen_volume=0,
            settlement_locked_volume=0, available_volume=0,
            average_open_price=Decimal("0"), position_cost=Decimal("0"),
            market_value=Decimal("0"), mark_price=None, mark_time=None,
            mark_source_event_id=None, daily_pnl_base_cost=Decimal("0"),
            yesterday_pnl_base_cost=Decimal("0"),
            today_pnl_base_cost=Decimal("0"), daily_pnl_base_established=True,
            used_margin=Decimal("0"), initial_occupied_margin=Decimal("0"),
            realtime_required_margin=Decimal("0"), option_market_value=Decimal("0"),
            margin_rule_id=None, margin_rule_version=None, margin_rule_snapshot=None,
            margin_price_mode=None, margin_underlying_price=None, margin_option_price=None,
            margin_calculated_at=None, multiplier_snapshot=Decimal(instrument.contract_multiplier),
            realized_pnl=Decimal("0"), unrealized_pnl=Decimal("0"),
            daily_position_pnl=Decimal("0"), daily_close_pnl=Decimal("0"),
            trading_day=order.trading_day, created_at=now, updated_at=now,
        )
        self.position_repository.add(db, position)
        db.flush()
        return position

    def _update_order(self, order, *, price: Decimal, volume: int, now: datetime) -> None:
        before = order.traded_volume
        order.traded_volume += volume
        order.remaining_volume -= volume
        order.average_price = quantize_money((Decimal(before) * (order.average_price or Decimal("0")) + Decimal(volume) * price) / Decimal(order.traded_volume))
        order.status = OrderStatus.FILLED.value if order.remaining_volume == 0 else OrderStatus.PARTIALLY_FILLED.value
        order.updated_at = now

    @staticmethod
    def _refresh_account_pnl_facts(account) -> None:
        """Keep PostgreSQL account facts internally consistent per fill."""

        account.daily_pnl = quantize_money(
            Decimal(account.daily_position_pnl)
            + Decimal(account.daily_close_pnl)
            - Decimal(account.daily_commission)
        )
        account.cumulative_net_pnl = quantize_money(
            Decimal(account.realized_pnl)
            + Decimal(account.unrealized_pnl)
            - Decimal(account.used_commission)
        )

    def _create_events(self, db: Session, *, order, trade: Trade, account, position: Position, now: datetime) -> None:
        event_id = self._id("CSE")
        self.outbox_repository.create_event(db, event_id=event_id, aggregate_type="TRADE", aggregate_id=trade.trade_id, event_type="TRADE_CREATED", created_at=now, payload={"event_id": event_id, "event_type": "TRADE_CREATED", "trade_id": trade.trade_id, "order_id": order.order_id, "account_id": order.account_id, "account_type": "SECURITIES_CASH", "instrument_type": order.instrument_type, "exchange_id": order.exchange_id, "symbol": order.symbol, "order_book_id": order.order_book_id, "direction": order.direction, "trade_price": format(trade.trade_price, "f"), "trade_volume": trade.trade_volume, "turnover": format(trade.turnover, "f"), "commission": format(trade.commission, "f"), "realized_pnl": format(trade.realized_pnl, "f"), "market_event_id": trade.market_event_id, "updated_at": now.isoformat()})
        event_id = self._id("CSE")
        self.outbox_repository.create_event(db, event_id=event_id, aggregate_type="ORDER", aggregate_id=order.order_id, event_type="ORDER_FILLED" if order.status == OrderStatus.FILLED.value else "ORDER_PARTIALLY_FILLED", created_at=now, payload={"event_id": event_id, "event_type": "ORDER_FILLED" if order.status == OrderStatus.FILLED.value else "ORDER_PARTIALLY_FILLED", "order_id": order.order_id, "account_id": order.account_id, "account_type": "SECURITIES_CASH", "instrument_type": order.instrument_type, "exchange_id": order.exchange_id, "symbol": order.symbol, "order_book_id": order.order_book_id, "direction": order.direction, "status": order.status, "traded_volume": order.traded_volume, "remaining_volume": order.remaining_volume, "average_price": format(order.average_price, "f"), "frozen_cash": format(order.frozen_cash, "f"), "frozen_commission": format(order.frozen_commission, "f"), "frozen_position_volume": order.frozen_position_volume, "updated_at": now.isoformat()})
        self.realtime_events.create_position_updated(db, position=position, occurred_at=now)
        self.realtime_events.create_account_updated(
            db,
            account=account,
            occurred_at=now,
            account_type="SECURITIES_CASH",
            fact_reason="CASH_SECURITY_TRADE_SETTLED",
        )

    def settle(self, db: Session, *, order_id: str, market_event_id: str, market_stream_message_id: str, tick_event_time: datetime, match: CashSecurityMatchResult) -> CashSecuritySettlementResult:
        if not match.matched or match.fill_price is None or match.fill_volume <= 0:
            return CashSecuritySettlementResult(None, order_id, "NOT_MATCHED")
        try:
            order = self.order_repository.get_by_order_id_for_update(db, order_id)
            if order is None:
                db.rollback()
                return CashSecuritySettlementResult(None, order_id, "ORDER_NOT_FOUND")
            existing = self.trade_repository.get_by_order_market_event(db, order_id=order_id, market_event_id=market_event_id)
            if existing is not None:
                db.commit()
                return CashSecuritySettlementResult(existing.trade_id, order_id, "IDEMPOTENT")
            if order.instrument_type not in CASH_SECURITY_TYPES or order.offset_flag is not None or order.status not in ACTIVE_STATUSES or order.remaining_volume <= 0:
                db.commit()
                return CashSecuritySettlementResult(None, order_id, "ORDER_NOT_ACTIVE")
            volume = min(order.remaining_volume, match.fill_volume)
            account = self.account_repository.get_by_account_id_for_update(db, order.account_id)
            self._cash_account(account)
            instrument = self.instrument_repository.get(db, order.exchange_id, order.symbol)
            if instrument is None or instrument.instrument_type != order.instrument_type or not instrument.is_active or not instrument.is_tradeable:
                raise DataAccessError("现金证券合约事实不一致", error_code="CASH_SECURITY_INSTRUMENT_INVALID")
            now = utc_now()
            trade_id = self._id("CST")
            turnover = quantize_money(match.fill_price * Decimal(volume) * Decimal(instrument.contract_multiplier))
            # Trade 行必须先于费用明细进入 Session 并落库：_settle_fees 会把
            # 外键指向 trade.trade_id 的 CashSecurityTradeFeeComponent 行加入
            # Session，而 _position_for_update 在新建持仓时会显式 flush、提交
            # 时也会 flush。两个模型之间没有 ORM relationship，SQLAlchemy 无法
            # 感知插入顺序，若费用子行先于 trade 行插入，PostgreSQL 会以
            # cash_security_trade_fee_component_trade_id_fkey 拒绝整个事务。
            # 佣金与已实现盈亏在下方方向分支里才确定，先以占位值插入，
            # 提交前在同一事务内更新为最终值。
            trade = Trade(
                trade_id=trade_id,
                order_id=order.order_id,
                account_id=order.account_id,
                market_event_id=market_event_id,
                market_stream_message_id=market_stream_message_id,
                order_book_id=order.order_book_id,
                exchange_id=order.exchange_id,
                symbol=order.symbol,
                trading_day=order.trading_day,
                instrument_type=order.instrument_type,
                direction=order.direction,
                offset_flag=None,
                trade_price=match.fill_price,
                trade_volume=volume,
                turnover=turnover,
                margin=Decimal("0"),
                premium_cash_flow=Decimal("0"),
                margin_rule_id=None,
                margin_rule_version=None,
                margin_calculation_version=None,
                commission=Decimal("0"),
                realized_pnl=Decimal("0"),
                daily_close_pnl=Decimal("0"),
                trade_time=tick_event_time,
                created_at=now,
            )
            self.trade_repository.add(db, trade)
            db.flush()
            commission = self._settle_fees(db, order=order, trade_id=trade_id, price=match.fill_price, volume=volume, now=now)
            position = self._position_for_update(db, order=order, instrument=instrument, now=now)
            realized_pnl = Decimal("0")
            if order.direction == OrderDirection.BUY.value:
                remaining_after = order.remaining_volume - volume
                prior_frozen_cash = order.frozen_cash
                prior_frozen_commission = order.frozen_commission
                remaining_cash = quantize_money(order.limit_price * Decimal(remaining_after) * Decimal(instrument.contract_multiplier))
                # 接单时冻结的预计资金必须保留到剩余数量全部成交或撤单。单笔成交
                # 最多消耗该预计冻结；若实际手续费更高，仍需在下方校验实时可用资金。
                # 全部成交时剩余数量为0，预计冻结必须一次性释放：逐笔实际手续费
                # 与预计值之间的舍入差不能留在 FILLED 订单和账户冻结字段上，
                # 否则日终对账会把残留当成无法解释的冻结资金。
                remaining_commission = (
                    Decimal("0")
                    if remaining_after == 0
                    else max(
                        Decimal("0"),
                        quantize_money(order.frozen_commission - commission),
                    )
                )
                available_after = quantize_money(
                    account.available_cash
                    + prior_frozen_cash
                    + prior_frozen_commission
                    - turnover
                    - commission
                    - remaining_cash
                    - remaining_commission
                )
                if available_after < Decimal("0"):
                    raise BusinessRuleError("现金证券成交资金不足", error_code="CASH_SECURITY_SETTLEMENT_CASH_INSUFFICIENT")
                if (
                    account.frozen_cash < prior_frozen_cash
                    or account.frozen_commission < prior_frozen_commission
                ):
                    raise DataAccessError(
                        "现金证券账户冻结事实不一致",
                        error_code="CASH_SECURITY_BUY_FREEZE_INCONSISTENT",
                    )
                account.available_cash = available_after
                account.frozen_cash = quantize_money(
                    account.frozen_cash - prior_frozen_cash + remaining_cash
                )
                account.frozen_commission = quantize_money(
                    account.frozen_commission
                    - prior_frozen_commission
                    + remaining_commission
                )
                account.cash_balance = quantize_money(account.cash_balance - turnover - commission)
                order.frozen_cash = remaining_cash
                order.frozen_commission = remaining_commission
                CashSecurityPositionService.apply_buy(
                    position,
                    instrument_type=order.instrument_type,
                    volume=volume,
                    turnover=turnover,
                )
            elif order.direction == OrderDirection.SELL.value:
                if order.frozen_position_volume < volume:
                    raise DataAccessError("现金证券卖出冻结事实不一致", error_code="CASH_SECURITY_SELL_FREEZE_INCONSISTENT")
                cost = CashSecurityPositionService.apply_sell(
                    position, instrument_type=order.instrument_type, volume=volume
                )
                # 已实现盈亏按毛额记录。手续费单独计入 daily_commission /
                # used_commission，且已经反映在现金变动中；这里再扣一次会导致
                # 当日和累计盈亏重复扣减手续费。
                realized_pnl = quantize_money(turnover - cost)
                order.frozen_position_volume -= volume
                account.available_cash = quantize_money(account.available_cash + turnover - commission)
                account.cash_balance = quantize_money(account.cash_balance + turnover - commission)
                account.realized_pnl = quantize_money(account.realized_pnl + realized_pnl)
                account.daily_close_pnl = quantize_money(
                    account.daily_close_pnl + realized_pnl
                )
                position.realized_pnl = quantize_money(position.realized_pnl + realized_pnl)
                position.daily_close_pnl = quantize_money(
                    position.daily_close_pnl + realized_pnl
                )
                if position.total_volume == 0:
                    # 已平完的现金证券持仓不能保留可交易市值或浮动盈亏；即使估值
                    # Worker 尚未消费对应的持久化事实事件，也要在成交事务中清零。
                    position.market_value = Decimal("0")
                    position.unrealized_pnl = Decimal("0")
                    position.daily_position_pnl = Decimal("0")
                    position.daily_pnl_base_cost = Decimal("0")
            else:
                raise DataAccessError("现金证券订单方向无效", error_code="CASH_SECURITY_DIRECTION_INVALID")
            account.used_commission = quantize_money(account.used_commission + commission)
            account.daily_commission = quantize_money(account.daily_commission + commission)
            self._refresh_account_pnl_facts(account)
            account.updated_at = now
            position.updated_at = now
            self._update_order(order, price=match.fill_price, volume=volume, now=now)
            # 佣金与已实现盈亏依赖方向分支，在 Trade 行落库后才确定；提交前
            # 在同一事务内更新为最终值，_create_events 的事件负载也依赖它们。
            trade.commission = commission
            trade.realized_pnl = realized_pnl
            trade.daily_close_pnl = realized_pnl
            self._create_events(db, order=order, trade=trade, account=account, position=position, now=now)
            db.commit()
            return CashSecuritySettlementResult(trade.trade_id, order_id, "SETTLED")
        except IntegrityError:
            db.rollback()
            existing = self.trade_repository.get_by_order_market_event(db, order_id=order_id, market_event_id=market_event_id)
            if existing is not None:
                return CashSecuritySettlementResult(existing.trade_id, order_id, "IDEMPOTENT")
            raise
        except Exception:
            db.rollback()
            raise
