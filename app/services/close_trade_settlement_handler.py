from decimal import Decimal

from sqlalchemy.orm import Session

from app.common.decimal_utils import quantize_money
from app.common.exceptions import DataAccessError
from app.enums.order_enums import (
    OrderStatus,
    PositionDetailStatus,
    PositionDirection,
    PositionFreezeAllocationStatus,
)
from app.models.trade import Trade
from app.repositories.position_freeze_allocation_repository import (
    PositionFreezeAllocationRepository,
)
from app.repositories.position_repository import PositionRepository
from app.repositories.trade_repository import TradeRepository
from app.services.margin_release_calculator import MarginReleaseCalculator
from app.services.realized_pnl_calculator import RealizedPnlCalculator


class CloseTradeSettlementHandler:
    """在已锁定Order和Account后执行一笔平仓成交的领域更新。"""

    def __init__(
        self,
        *,
        position_repository: PositionRepository,
        allocation_repository: PositionFreezeAllocationRepository,
        trade_repository: TradeRepository,
        pnl_calculator: RealizedPnlCalculator | None = None,
        margin_calculator: MarginReleaseCalculator | None = None,
    ):
        self.position_repository = position_repository
        self.allocation_repository = allocation_repository
        self.trade_repository = trade_repository
        self.pnl_calculator = pnl_calculator or RealizedPnlCalculator()
        self.margin_calculator = margin_calculator or MarginReleaseCalculator()

    @staticmethod
    def _allocate_commission(
        amount: Decimal,
        *,
        fill_volume: int,
        remaining_volume: int,
    ) -> Decimal:
        if fill_volume == remaining_volume:
            return quantize_money(amount)
        return quantize_money(
            amount * Decimal(fill_volume) / Decimal(remaining_volume)
        )

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
        """消费本订单自己的冻结分配并更新成交、账户和剩余持仓。"""

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

        # 先锁全部逐笔明细，再锁本订单Allocation，锁顺序与撤单保持一致。
        details = self.position_repository.list_details_for_update(
            db,
            position_id=position.position_id,
        )
        detail_map = {item.position_detail_id: item for item in details}
        allocations = self.allocation_repository.list_by_order_for_update(
            db,
            order.order_id,
        )
        if sum(item.remaining_frozen_volume for item in allocations) < fill_volume:
            raise DataAccessError(
                "平仓订单冻结分配数量不足",
                error_code="CLOSE_ALLOCATION_INCONSISTENT",
            )
        if order.frozen_position_volume < fill_volume:
            raise DataAccessError(
                "平仓订单冻结持仓数量不足",
                error_code="CLOSE_POSITION_INCONSISTENT",
            )

        remaining_to_consume = fill_volume
        released_margin = Decimal("0.000000")
        realized_pnl = Decimal("0.000000")
        for allocation in allocations:
            if remaining_to_consume == 0:
                break
            if allocation.remaining_frozen_volume <= 0:
                continue
            detail = detail_map.get(allocation.position_detail_id)
            if detail is None:
                raise DataAccessError(
                    "冻结分配对应持仓明细不存在",
                    error_code="CLOSE_ALLOCATION_INCONSISTENT",
                )
            consumed = min(
                allocation.remaining_frozen_volume,
                remaining_to_consume,
            )
            if detail.frozen_volume < consumed or detail.remaining_volume < consumed:
                raise DataAccessError(
                    "持仓明细冻结数量不一致",
                    error_code="CLOSE_POSITION_INCONSISTENT",
                )
            margin = self.margin_calculator.calculate(
                remaining_margin=detail.remaining_margin,
                close_volume=consumed,
                remaining_volume_before_close=detail.remaining_volume,
            )
            pnl = self.pnl_calculator.calculate(
                close_direction=order.direction,
                open_price=detail.open_price,
                close_price=fill_price,
                volume=consumed,
                contract_multiplier=Decimal(instrument.contract_multiplier),
            )
            released_margin = quantize_money(released_margin + margin)
            realized_pnl = quantize_money(realized_pnl + pnl)

            detail.remaining_volume -= consumed
            detail.frozen_volume -= consumed
            detail.remaining_margin = quantize_money(
                detail.remaining_margin - margin
            )
            detail.status = (
                PositionDetailStatus.CLOSED.value
                if detail.remaining_volume == 0
                else PositionDetailStatus.OPEN.value
            )
            detail.updated_at = now

            allocation.remaining_frozen_volume -= consumed
            allocation.consumed_volume += consumed
            allocation.status = (
                PositionFreezeAllocationStatus.CONSUMED.value
                if allocation.remaining_frozen_volume == 0
                else PositionFreezeAllocationStatus.ACTIVE.value
            )
            allocation.updated_at = now
            remaining_to_consume -= consumed

        if remaining_to_consume:
            raise DataAccessError(
                "平仓成交未完全消费冻结分配",
                error_code="CLOSE_ALLOCATION_INCONSISTENT",
            )

        allocated_commission = self._allocate_commission(
            order.frozen_commission,
            fill_volume=fill_volume,
            remaining_volume=remaining_before,
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
            direction=order.direction,
            offset_flag=order.offset_flag,
            trade_price=fill_price,
            trade_volume=fill_volume,
            turnover=turnover,
            margin=released_margin,
            commission=allocated_commission,
            realized_pnl=realized_pnl,
            trade_time=command.tick_event_time,
            created_at=now,
        )
        self.trade_repository.add(db, trade)

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
            order.frozen_commission - allocated_commission
        )
        order.frozen_position_volume -= fill_volume
        order.updated_at = now

        if (
            account.used_margin < released_margin
            or account.frozen_commission < allocated_commission
            or position.used_margin < released_margin
            or position.frozen_volume < fill_volume
        ):
            raise DataAccessError(
                "平仓成交账户或持仓资源不一致",
                error_code="CLOSE_RESOURCE_INCONSISTENT",
            )

        account.used_margin = quantize_money(
            account.used_margin - released_margin
        )
        account.frozen_commission = quantize_money(
            account.frozen_commission - allocated_commission
        )
        account.used_commission = quantize_money(
            account.used_commission + allocated_commission
        )
        account.realized_pnl = quantize_money(
            account.realized_pnl + realized_pnl
        )
        account.cash_balance = quantize_money(
            account.cash_balance + realized_pnl - allocated_commission
        )
        account.available_cash = quantize_money(
            account.available_cash + released_margin + realized_pnl
        )
        account.equity = quantize_money(
            account.cash_balance + account.unrealized_pnl
        )
        account.daily_pnl = quantize_money(
            account.daily_pnl + realized_pnl - allocated_commission
        )
        account.updated_at = now

        position.total_volume -= fill_volume
        position.frozen_volume -= fill_volume
        position.used_margin = quantize_money(
            position.used_margin - released_margin
        )
        position.realized_pnl = quantize_money(
            position.realized_pnl + realized_pnl
        )
        position.today_volume = sum(
            item.remaining_volume
            for item in details
            if item.open_trading_day == order.trading_day
        )
        position.yesterday_volume = sum(
            item.remaining_volume
            for item in details
            if item.open_trading_day < order.trading_day
        )
        position.available_volume = (
            position.total_volume - position.frozen_volume
        )
        if position.total_volume == 0:
            position.average_open_price = Decimal("0.000000")
            position.position_cost = Decimal("0.000000")
            position.used_margin = Decimal("0.000000")
            position.unrealized_pnl = Decimal("0.000000")
        else:
            remaining_cost = sum(
                (
                    item.open_price
                    * Decimal(item.remaining_volume)
                    * Decimal(instrument.contract_multiplier)
                    for item in details
                ),
                Decimal("0"),
            )
            weighted_price = sum(
                (
                    item.open_price * Decimal(item.remaining_volume)
                    for item in details
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
        ):
            raise DataAccessError(
                "平仓后持仓数量不守恒",
                error_code="CLOSE_POSITION_INCONSISTENT",
            )
        position.updated_at = now
        return trade
