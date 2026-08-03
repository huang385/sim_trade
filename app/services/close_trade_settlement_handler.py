from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal
from uuid import uuid4

from sqlalchemy.orm import Session

from app.common.decimal_utils import quantize_money
from app.common.exceptions import DataAccessError
from app.common.time_utils import utc_now
from app.core.config import settings
from app.enums.account_enums import AccountRiskState
from app.enums.order_enums import (
    OffsetFlag,
    OrderStatus,
    PositionDetailStatus,
    PositionDirection,
    PositionFreezeAllocationStatus,
)
from app.enums.option_enums import InstrumentType
from app.models.trade import Trade
from app.models.trade_position_allocation import TradePositionAllocation
from app.repositories.position_freeze_allocation_repository import (
    PositionFreezeAllocationRepository,
)
from app.repositories.position_repository import PositionRepository
from app.repositories.trade_position_allocation_repository import (
    TradePositionAllocationRepository,
)
from app.repositories.trade_repository import TradeRepository
from app.services.fee_calculator import (
    FeeBucketEntry,
    FeeBucketKey,
    FeeCalculator,
)
from app.services.margin_release_calculator import MarginReleaseCalculator
from app.services.account_valuation_calculator import AccountValuationCalculator
from app.services.pnl_calculator import PnlCalculator
from app.services.realized_pnl_calculator import RealizedPnlCalculator


def generate_trade_position_allocation_id() -> str:
    """生成平仓成交逐笔持仓明细编号。"""

    return f"TPA{utc_now().strftime('%Y%m%d')}{uuid4().hex[:16].upper()}"


@dataclass(frozen=True)
class _CloseConsumption:
    """校验完成后暂存一条 Allocation 的本次消费计算结果。"""

    allocation: object
    detail: object
    volume: int
    released_margin: Decimal
    released_frozen_commission: Decimal
    actual_commission: Decimal
    realized_pnl: Decimal
    daily_close_pnl: Decimal


class CloseTradeSettlementHandler:
    """在已锁定 Order 和 Account 后执行一笔平仓成交的领域更新。"""

    def __init__(
        self,
        *,
        position_repository: PositionRepository,
        allocation_repository: PositionFreezeAllocationRepository,
        trade_repository: TradeRepository,
        trade_position_allocation_repository: (
            TradePositionAllocationRepository | None
        ) = None,
        fee_calculator: FeeCalculator | None = None,
        pnl_calculator: RealizedPnlCalculator | None = None,
        daily_pnl_calculator: PnlCalculator | None = None,
        margin_calculator: MarginReleaseCalculator | None = None,
        trade_position_allocation_id_factory: Callable[[], str] = (
            generate_trade_position_allocation_id
        ),
    ):
        self.position_repository = position_repository
        self.allocation_repository = allocation_repository
        self.trade_repository = trade_repository
        self.trade_position_allocation_repository = (
            trade_position_allocation_repository
            or TradePositionAllocationRepository()
        )
        self.fee_calculator = fee_calculator or FeeCalculator()
        self.pnl_calculator = pnl_calculator or RealizedPnlCalculator()
        self.daily_pnl_calculator = (
            daily_pnl_calculator or PnlCalculator()
        )
        self.margin_calculator = margin_calculator or MarginReleaseCalculator()
        self.trade_position_allocation_id_factory = (
            trade_position_allocation_id_factory
        )

    @staticmethod
    def _allocate_frozen_commission(
        amount: Decimal,
        *,
        fill_volume: int,
        remaining_volume: int,
    ) -> Decimal:
        """按单条 Allocation 剩余量释放预计手续费，最后一次消费全部尾差。"""

        if fill_volume == remaining_volume:
            return quantize_money(amount)
        return quantize_money(
            amount * Decimal(fill_volume) / Decimal(remaining_volume)
        )

    @staticmethod
    def _validate_allocation_balance(allocation) -> None:
        """在修改任何 ORM 字段前检查单条 Allocation 的数量和手续费守恒。"""

        if (
            allocation.original_frozen_volume
            != allocation.remaining_frozen_volume
            + allocation.consumed_volume
            + allocation.released_volume
            or allocation.original_frozen_volume <= 0
            or allocation.remaining_frozen_volume < 0
            or allocation.consumed_volume < 0
            or allocation.released_volume < 0
        ):
            raise DataAccessError(
                "平仓冻结分配数量不守恒",
                error_code="CLOSE_ALLOCATION_INCONSISTENT",
            )

        original_commission = quantize_money(
            allocation.original_frozen_commission
        )
        commission_parts = quantize_money(
            allocation.remaining_frozen_commission
            + allocation.consumed_commission
            + allocation.released_commission
        )
        if (
            original_commission != commission_parts
            or allocation.remaining_frozen_commission < Decimal("0")
            or allocation.consumed_commission < Decimal("0")
            or allocation.released_commission < Decimal("0")
        ):
            raise DataAccessError(
                "平仓冻结分配手续费不守恒",
                error_code="CLOSE_ALLOCATION_INCONSISTENT",
            )

    def _validate_before_mutation(
        self,
        *,
        order,
        position,
        details,
        allocations,
        fill_volume: int,
        expected_position_direction: str,
    ) -> dict[str, object]:
        """
        严格验证订单、持仓明细和 Allocation 的完整恒等式。

        本方法只读取对象，不修改任何字段。任一异常都会在 Trade、账户、
        持仓、Allocation 和 Outbox 发生变化之前终止结算。
        """

        if fill_volume <= 0 or fill_volume > order.remaining_volume:
            raise DataAccessError(
                "平仓成交数量超出订单剩余量",
                error_code="CLOSE_ALLOCATION_INCONSISTENT",
            )
        if (
            order.total_volume
            != order.traded_volume
            + order.remaining_volume
            + order.cancelled_volume
        ):
            raise DataAccessError(
                "平仓订单数量不守恒",
                error_code="CLOSE_ALLOCATION_INCONSISTENT",
            )
        if position.direction != expected_position_direction:
            raise DataAccessError(
                "平仓订单持仓方向不一致",
                error_code="CLOSE_ALLOCATION_INCONSISTENT",
            )
        if not allocations:
            raise DataAccessError(
                "活动平仓订单不存在冻结分配",
                error_code="CLOSE_ALLOCATION_INCONSISTENT",
            )

        detail_map = {item.position_detail_id: item for item in details}
        allocation_remaining_volume = 0
        allocation_remaining_commission = Decimal("0")
        original_bucket_entries: list[FeeBucketEntry] = []
        original_bucket_actual: dict[FeeBucketKey, Decimal] = {}
        for allocation in allocations:
            self._validate_allocation_balance(allocation)
            if (
                allocation.order_id != order.order_id
                or allocation.position_id != position.position_id
                or allocation.account_id != order.account_id
                or allocation.exchange_id != order.exchange_id
                or allocation.symbol != order.symbol
            ):
                raise DataAccessError(
                    "平仓冻结分配归属不一致",
                    error_code="CLOSE_ALLOCATION_INCONSISTENT",
                )
            if allocation.resolved_offset_flag not in {
                OffsetFlag.CLOSE_TODAY.value,
                OffsetFlag.CLOSE_YESTERDAY.value,
            }:
                raise DataAccessError(
                    "平仓冻结分配缺少明确平今平昨标志",
                    error_code="CLOSE_ALLOCATION_INCONSISTENT",
                )
            detail = detail_map.get(allocation.position_detail_id)
            if detail is None:
                raise DataAccessError(
                    "冻结分配对应持仓明细不存在",
                    error_code="CLOSE_ALLOCATION_INCONSISTENT",
                )
            if (
                detail.position_id != position.position_id
                or detail.account_id != order.account_id
                or detail.exchange_id != order.exchange_id
                or detail.symbol != order.symbol
                or detail.direction != expected_position_direction
            ):
                raise DataAccessError(
                    "冻结分配对应持仓明细归属不一致",
                    error_code="CLOSE_ALLOCATION_INCONSISTENT",
                )
            expected_resolved_offset = (
                OffsetFlag.CLOSE_TODAY.value
                if detail.open_trading_day == order.trading_day
                else (
                    OffsetFlag.CLOSE_YESTERDAY.value
                    if detail.open_trading_day < order.trading_day
                    else None
                )
            )
            if allocation.resolved_offset_flag != expected_resolved_offset:
                raise DataAccessError(
                    "冻结分配平今平昨标志与持仓日期不一致",
                    error_code="CLOSE_ALLOCATION_INCONSISTENT",
                )
            bucket_key = FeeBucketKey(
                resolved_offset_flag=allocation.resolved_offset_flag,
                commission_type=allocation.commission_type,
                commission_parameter=allocation.commission_parameter,
                commission_contract_multiplier=(
                    allocation.commission_contract_multiplier
                ),
            )
            original_bucket_entries.append(
                FeeBucketEntry(
                    key=bucket_key,
                    volume=allocation.original_frozen_volume,
                )
            )
            original_bucket_actual[bucket_key] = quantize_money(
                original_bucket_actual.get(
                    bucket_key,
                    Decimal("0.000000"),
                )
                + allocation.original_frozen_commission
            )
            if (
                allocation.remaining_frozen_volume > detail.frozen_volume
                or allocation.remaining_frozen_volume > detail.remaining_volume
            ):
                raise DataAccessError(
                    "持仓明细冻结数量不一致",
                    error_code="CLOSE_ALLOCATION_INCONSISTENT",
                )
            allocation_remaining_volume += allocation.remaining_frozen_volume
            allocation_remaining_commission += (
                allocation.remaining_frozen_commission
            )

        # 原始预计手续费只按桶汇总验证。单条 Allocation 可能承担桶级
        # 0.000001 尾差，不能再要求其金额等于单独重算的结果。
        expected_shares = self.fee_calculator.calculate_bucket_allocations(
            price=order.limit_price,
            entries=original_bucket_entries,
        )
        expected_bucket_totals: dict[FeeBucketKey, Decimal] = {}
        for entry, share in zip(
            original_bucket_entries,
            expected_shares,
            strict=True,
        ):
            expected_bucket_totals[entry.key] = quantize_money(
                expected_bucket_totals.get(
                    entry.key,
                    Decimal("0.000000"),
                )
                + share
            )
        if expected_bucket_totals != original_bucket_actual:
            raise DataAccessError(
                "平仓冻结分配手续费桶汇总不一致",
                error_code="CLOSE_ALLOCATION_INCONSISTENT",
            )

        if (
            allocation_remaining_volume != order.frozen_position_volume
            or order.frozen_position_volume != order.remaining_volume
        ):
            raise DataAccessError(
                "平仓订单冻结数量不一致",
                error_code="CLOSE_ALLOCATION_INCONSISTENT",
            )
        if quantize_money(allocation_remaining_commission) != quantize_money(
            order.frozen_commission
        ):
            raise DataAccessError(
                "平仓订单冻结手续费与分配不一致",
                error_code="CLOSE_ALLOCATION_INCONSISTENT",
            )
        return detail_map

    def apply(
        self,
        db: Session,
        *,
        order,
        account,
        instrument,
        command,
        fill_volume: int,
        fill_price: Decimal,
        remaining_before: int,
        traded_before: int,
        average_before: Decimal,
        trade_id: str,
        now,
    ) -> Trade:
        """消费本订单冻结分配并原子更新成交、账户和剩余持仓。"""

        instrument_type = InstrumentType(
            getattr(order, "instrument_type", InstrumentType.FUTURES.value)
        )
        is_option = instrument_type in {
            InstrumentType.FUTURES_OPTION,
            InstrumentType.INDEX_OPTION,
        }
        order_frozen_cash = Decimal(
            getattr(order, "frozen_cash", Decimal("0"))
        )
        released_frozen_cash = (
            self._allocate_frozen_commission(
                order_frozen_cash,
                fill_volume=fill_volume,
                remaining_volume=remaining_before,
            )
            if is_option
            else Decimal("0")
        )

        position_direction = (
            PositionDirection.LONG.value
            if order.direction == "SELL"
            else PositionDirection.SHORT.value
        )
        position = self.position_repository.get_for_update(
            db,
            account_id=order.account_id,
            exchange_id=order.exchange_id,
            symbol=order.symbol,
            direction=position_direction,
        )
        if position is None:
            raise DataAccessError(
                "平仓成交对应持仓不存在",
                error_code="CLOSE_POSITION_NOT_FOUND",
            )

        # 锁顺序与撤单保持一致：Order -> Account -> Position ->
        # 当前订单 Allocation -> Allocation引用的PositionDetail。
        allocations = self.allocation_repository.list_by_order_for_update(
            db,
            order.order_id,
        )
        allocation_detail_ids = list(
            dict.fromkeys(
                item.position_detail_id for item in allocations
            )
        )
        details = self.position_repository.list_details_by_ids_for_update(
            db,
            position_id=position.position_id,
            position_detail_ids=allocation_detail_ids,
        )
        detail_map = self._validate_before_mutation(
            order=order,
            position=position,
            details=details,
            allocations=allocations,
            fill_volume=fill_volume,
            expected_position_direction=position_direction,
        )

        # 先纯计算本次会消费的所有明细。实际手续费按 fill_price 和每条
        # Allocation 的规则快照分别计算，普通 CLOSE 跨今昨时不会平均分摊。
        remaining_to_consume = fill_volume
        consumptions: list[_CloseConsumption] = []
        for allocation in allocations:
            if remaining_to_consume == 0:
                break
            if allocation.remaining_frozen_volume <= 0:
                continue
            detail = detail_map[allocation.position_detail_id]
            consumed = min(
                allocation.remaining_frozen_volume,
                remaining_to_consume,
            )
            released_margin = self.margin_calculator.calculate(
                remaining_margin=detail.remaining_margin,
                close_volume=consumed,
                remaining_volume_before_close=detail.remaining_volume,
            )
            realized_pnl = self.pnl_calculator.calculate(
                close_direction=order.direction,
                open_price=detail.open_price,
                close_price=fill_price,
                volume=consumed,
                contract_multiplier=Decimal(instrument.contract_multiplier),
            )
            daily_close_pnl = (
                self.daily_pnl_calculator.calculate_close(
                    position_direction=position_direction,
                    close_price=fill_price,
                    open_price=detail.open_price,
                    pnl_base_price=detail.pnl_base_price,
                    volume=consumed,
                    contract_multiplier=Decimal(
                        instrument.contract_multiplier
                    ),
                ).daily_close_pnl
            )
            released_frozen_commission = (
                self._allocate_frozen_commission(
                    allocation.remaining_frozen_commission,
                    fill_volume=consumed,
                    remaining_volume=allocation.remaining_frozen_volume,
                )
            )
            consumptions.append(
                _CloseConsumption(
                    allocation=allocation,
                    detail=detail,
                    volume=consumed,
                    released_margin=released_margin,
                    released_frozen_commission=(
                        released_frozen_commission
                    ),
                    actual_commission=Decimal("0.000000"),
                    realized_pnl=realized_pnl,
                    daily_close_pnl=daily_close_pnl,
                )
            )
            remaining_to_consume -= consumed

        if remaining_to_consume:
            raise DataAccessError(
                "平仓成交未完全消费冻结分配",
                error_code="CLOSE_ALLOCATION_INCONSISTENT",
            )

        # 实际手续费同样按“本次 Trade 消费的手续费桶”汇总计算，再按
        # FIFO消费顺序分配给逐笔审计明细，最后一条吸收六位量化尾差。
        actual_commissions = (
            self.fee_calculator.calculate_bucket_allocations(
                price=fill_price,
                entries=[
                    FeeBucketEntry(
                        key=FeeBucketKey(
                            resolved_offset_flag=(
                                item.allocation.resolved_offset_flag
                            ),
                            commission_type=(
                                item.allocation.commission_type
                            ),
                            commission_parameter=(
                                item.allocation.commission_parameter
                            ),
                            commission_contract_multiplier=(
                                item.allocation
                                .commission_contract_multiplier
                            ),
                        ),
                        volume=item.volume,
                    )
                    for item in consumptions
                ],
            )
        )
        consumptions = [
            replace(item, actual_commission=commission)
            for item, commission in zip(
                consumptions,
                actual_commissions,
                strict=True,
            )
        ]

        released_margin = quantize_money(
            sum(
                (item.released_margin for item in consumptions),
                Decimal("0"),
            )
        )
        released_frozen_commission = quantize_money(
            sum(
                (
                    item.released_frozen_commission
                    for item in consumptions
                ),
                Decimal("0"),
            )
        )
        actual_commission = quantize_money(
            sum(
                (item.actual_commission for item in consumptions),
                Decimal("0"),
            )
        )
        realized_pnl = quantize_money(
            sum(
                (item.realized_pnl for item in consumptions),
                Decimal("0"),
            )
        )
        daily_close_pnl = quantize_money(
            sum(
                (item.daily_close_pnl for item in consumptions),
                Decimal("0"),
            )
        )

        # 账户和持仓资源检查同样放在任何字段修改之前。
        if (
            account.used_margin < released_margin
            or account.frozen_commission < released_frozen_commission
            or Decimal(getattr(account, "frozen_cash", Decimal("0")))
            < released_frozen_cash
            or position.used_margin < released_margin
            or position.frozen_volume < fill_volume
        ):
            raise DataAccessError(
                "平仓成交账户或持仓资源不一致",
                error_code="CLOSE_RESOURCE_INCONSISTENT",
            )

        trade_position_allocations: list[TradePositionAllocation] = []
        for item in consumptions:
            allocation = item.allocation
            detail = item.detail

            detail.remaining_volume -= item.volume
            detail.frozen_volume -= item.volume
            detail.remaining_margin = quantize_money(
                detail.remaining_margin - item.released_margin
            )
            if is_option:
                detail.realtime_required_margin = max(
                    quantize_money(
                        detail.realtime_required_margin
                        - item.released_margin
                    ),
                    Decimal("0"),
                )
            detail.status = (
                PositionDetailStatus.CLOSED.value
                if detail.remaining_volume == 0
                else PositionDetailStatus.OPEN.value
            )
            detail.updated_at = now

            allocation.remaining_frozen_volume -= item.volume
            allocation.consumed_volume += item.volume
            allocation.remaining_frozen_commission = quantize_money(
                allocation.remaining_frozen_commission
                - item.released_frozen_commission
            )
            allocation.consumed_commission = quantize_money(
                allocation.consumed_commission
                + item.released_frozen_commission
            )
            allocation.status = (
                PositionFreezeAllocationStatus.CONSUMED.value
                if allocation.remaining_frozen_volume == 0
                else PositionFreezeAllocationStatus.ACTIVE.value
            )
            allocation.updated_at = now

            trade_position_allocations.append(
                TradePositionAllocation(
                    trade_position_allocation_id=(
                        self.trade_position_allocation_id_factory()
                    ),
                    trade_id=trade_id,
                    order_id=order.order_id,
                    allocation_id=allocation.allocation_id,
                    position_id=position.position_id,
                    position_detail_id=detail.position_detail_id,
                    account_id=order.account_id,
                    order_book_id=order.order_book_id,
                    exchange_id=order.exchange_id,
                    symbol=order.symbol,
                    resolved_offset_flag=(
                        allocation.resolved_offset_flag
                    ),
                    open_trading_day=detail.open_trading_day,
                    close_trading_day=order.trading_day,
                    open_price=detail.open_price,
                    close_price=fill_price,
                    close_volume=item.volume,
                    released_margin=item.released_margin,
                    commission=item.actual_commission,
                    realized_pnl=item.realized_pnl,
                    daily_close_pnl=item.daily_close_pnl,
                    created_at=now,
                )
            )

        turnover = quantize_money(
            fill_price
            * Decimal(fill_volume)
            * Decimal(instrument.contract_multiplier)
        )
        trade = Trade(
            trade_id=trade_id,
            order_id=order.order_id,
            account_id=order.account_id,
            market_event_id=command.market_event_id,
            market_stream_message_id=command.market_stream_message_id,
            order_book_id=order.order_book_id,
            exchange_id=order.exchange_id,
            symbol=order.symbol,
            trading_day=order.trading_day,
            instrument_type=instrument_type.value,
            direction=order.direction,
            offset_flag=order.offset_flag,
            trade_price=fill_price,
            trade_volume=fill_volume,
            turnover=turnover,
            margin=released_margin,
            premium_cash_flow=(
                turnover if order.direction == "SELL" else -turnover
            )
            if is_option
            else Decimal("0"),
            margin_rule_id=getattr(order, "margin_rule_id", None),
            margin_rule_version=getattr(order, "margin_rule_version", None),
            margin_calculation_version=getattr(
                order, "margin_calculation_version", None
            ),
            commission=actual_commission,
            realized_pnl=realized_pnl,
            daily_close_pnl=daily_close_pnl,
            trade_time=command.tick_event_time,
            created_at=now,
        )

        # 明细汇总必须与 Trade 完全一致；不满足时不允许把任何结果提交。
        if (
            sum(item.close_volume for item in trade_position_allocations)
            != trade.trade_volume
            or quantize_money(
                sum(
                    (
                        item.released_margin
                        for item in trade_position_allocations
                    ),
                    Decimal("0"),
                )
            )
            != trade.margin
            or quantize_money(
                sum(
                    (
                        item.realized_pnl
                        for item in trade_position_allocations
                    ),
                    Decimal("0"),
                )
            )
            != trade.realized_pnl
            or quantize_money(
                sum(
                    (
                        item.commission
                        for item in trade_position_allocations
                    ),
                    Decimal("0"),
                )
            )
            != trade.commission
        ):
            raise DataAccessError(
                "平仓成交逐笔明细汇总不一致",
                error_code="TRADE_POSITION_ALLOCATION_INCONSISTENT",
            )

        self.trade_repository.add(db, trade)
        for item in trade_position_allocations:
            self.trade_position_allocation_repository.add(db, item)

        new_traded = traded_before + fill_volume
        order.traded_volume = new_traded
        order.remaining_volume = remaining_before - fill_volume
        order.average_price = quantize_money(
            (
                average_before * Decimal(traded_before)
                + fill_price * Decimal(fill_volume)
            )
            / Decimal(new_traded)
        )
        order.status = (
            OrderStatus.FILLED.value
            if order.remaining_volume == 0
            else OrderStatus.PARTIALLY_FILLED.value
        )
        order.frozen_commission = quantize_money(
            order.frozen_commission - released_frozen_commission
        )
        if is_option:
            order.frozen_cash = quantize_money(
                order_frozen_cash - released_frozen_cash
            )
        order.frozen_position_volume -= fill_volume
        order.updated_at = now

        account.used_margin = quantize_money(
            account.used_margin - released_margin
        )
        account.frozen_commission = quantize_money(
            account.frozen_commission - released_frozen_commission
        )
        account.used_commission = quantize_money(
            account.used_commission + actual_commission
        )
        account.realized_pnl = quantize_money(
            account.realized_pnl + realized_pnl
        )
        account.daily_close_pnl = quantize_money(
            account.daily_close_pnl + daily_close_pnl
        )
        account.daily_commission = quantize_money(
            account.daily_commission + actual_commission
        )
        if is_option:
            # 期权现金只按权利金流和手续费变化。realized_pnl 是审计统计，
            # 不能像期货一样再次计入 cash_balance，否则会重复计算盈亏。
            premium_cash_flow = trade.premium_cash_flow
            account.frozen_cash = quantize_money(
                account.frozen_cash - released_frozen_cash
            )
            account.option_used_margin = quantize_money(
                account.option_used_margin - released_margin
            )
            account.cash_balance = quantize_money(
                account.cash_balance + premium_cash_flow - actual_commission
            )
            if position_direction == PositionDirection.LONG.value:
                account.long_option_market_value = max(
                    quantize_money(
                        account.long_option_market_value - turnover
                    ),
                    Decimal("0"),
                )
            else:
                account.short_option_market_value = max(
                    quantize_money(
                        account.short_option_market_value - turnover
                    ),
                    Decimal("0"),
                )
            valuation = AccountValuationCalculator.calculate(
                cash_balance=Decimal(account.cash_balance),
                futures_unrealized_pnl=Decimal(account.unrealized_pnl),
                long_option_market_value=Decimal(
                    account.long_option_market_value
                ),
                short_option_market_value=Decimal(
                    account.short_option_market_value
                ),
                used_margin=Decimal(account.used_margin),
                option_used_margin=Decimal(account.option_used_margin),
                option_realtime_required_margin=Decimal(
                    account.option_realtime_required_margin
                ),
                frozen_margin=Decimal(account.frozen_margin),
                frozen_cash=Decimal(account.frozen_cash),
                frozen_commission=Decimal(account.frozen_commission),
                option_collateral_ratio=settings.option_collateral_ratio,
            )
            account.available_cash = valuation.available_cash
            account.risk_available_cash = valuation.risk_available_cash
            account.equity = valuation.equity
            account.net_option_market_value = (
                valuation.net_option_market_value
            )
            if account.risk_available_cash >= Decimal("0"):
                account.risk_state = AccountRiskState.NORMAL.value
        else:
            account.cash_balance = quantize_money(
                account.cash_balance + realized_pnl - actual_commission
            )
            # 下单时预计手续费已经从可用资金扣除。成交时先释放本次预计值，
            # 再扣实际手续费，同时返还保证金并计入平仓盈亏。
            account.available_cash = quantize_money(
                account.available_cash
                + released_frozen_commission
                - actual_commission
                + released_margin
                + realized_pnl
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

        position.total_volume -= fill_volume
        position.frozen_volume -= fill_volume
        position.used_margin = quantize_money(
            position.used_margin - released_margin
        )
        if is_option:
            position.realtime_required_margin = max(
                quantize_money(
                    position.realtime_required_margin - released_margin
                ),
                Decimal("0"),
            )
        position.realized_pnl = quantize_money(
            position.realized_pnl + realized_pnl
        )
        position.daily_close_pnl = quantize_money(
            position.daily_close_pnl + daily_close_pnl
        )
        # 汇总重算只读取 remaining_volume > 0 的有效明细，不再读取或锁定
        # 与当前订单无关的已关闭历史明细。
        # 此时明细对象已经在内存中扣减，但 Position 的今仓/昨仓汇总尚未重算。
        # 禁止查询触发自动 flush，否则数据库会在汇总字段更新前检查日仓平衡约束。
        # SQL 可能把“本次刚好减到 0”的旧明细查回来，因此还要按对象的最新值
        # 做一次内存过滤；这样参与汇总的始终只有 remaining_volume > 0 的明细。
        with db.no_autoflush:
            queried_open_details = (
                self.position_repository.list_open_details(
                    db,
                    position_id=position.position_id,
                )
            )
        open_details = [
            item
            for item in queried_open_details
            if item.remaining_volume > 0
        ]
        position.today_volume = sum(
            item.remaining_volume
            for item in open_details
            if item.open_trading_day == order.trading_day
        )
        position.yesterday_volume = sum(
            item.remaining_volume
            for item in open_details
            if item.open_trading_day < order.trading_day
        )
        position.available_volume = (
            position.total_volume - position.frozen_volume
        )
        if position.total_volume == 0:
            position.average_open_price = Decimal("0.000000")
            position.position_cost = Decimal("0.000000")
            position.used_margin = Decimal("0.000000")
            position.realtime_required_margin = Decimal("0.000000")
            position.unrealized_pnl = Decimal("0.000000")
        else:
            remaining_cost = sum(
                (
                    item.open_price
                    * Decimal(item.remaining_volume)
                    * Decimal(instrument.contract_multiplier)
                    for item in open_details
                ),
                Decimal("0"),
            )
            weighted_price = sum(
                (
                    item.open_price * Decimal(item.remaining_volume)
                    for item in open_details
                ),
                Decimal("0"),
            )
            position.position_cost = quantize_money(remaining_cost)
            position.average_open_price = quantize_money(
                weighted_price / Decimal(position.total_volume)
            )

        if (
            position.total_volume
            != position.today_volume + position.yesterday_volume
            or position.available_volume
            != position.total_volume - position.frozen_volume
            or order.total_volume
            != order.traded_volume
            + order.remaining_volume
            + order.cancelled_volume
            or sum(
                item.remaining_frozen_volume for item in allocations
            )
            != order.frozen_position_volume
            or quantize_money(
                sum(
                    (
                        item.remaining_frozen_commission
                        for item in allocations
                    ),
                    Decimal("0"),
                )
            )
            != order.frozen_commission
        ):
            raise DataAccessError(
                "平仓后订单、持仓或冻结资源不守恒",
                error_code="CLOSE_POSITION_INCONSISTENT",
            )
        position.updated_at = now
        return trade
