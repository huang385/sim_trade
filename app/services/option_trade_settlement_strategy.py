from decimal import Decimal

from app.common.decimal_utils import quantize_money
from app.common.exceptions import DataAccessError
from app.core.config import settings
from app.enums.account_enums import AccountRiskState
from app.enums.order_enums import (
    OrderDirection,
    OrderStatus,
    PositionDetailStatus,
    PositionDirection,
)
from app.models.position import Position
from app.models.position_detail import PositionDetail
from app.models.trade import Trade
from app.services.account_valuation_calculator import (
    AccountValuationCalculator,
)


class OptionTradeSettlementStrategy:
    """处理期权开仓成交的权利金、保证金、账户和持仓变更。"""

    @staticmethod
    def _allocate(
        total: Decimal,
        *,
        fill_volume: int,
        remaining_volume: int,
    ) -> Decimal:
        if fill_volume == remaining_volume:
            return quantize_money(total)
        return quantize_money(
            total * Decimal(fill_volume) / Decimal(remaining_volume)
        )

    def apply_open(
        self,
        *,
        db,
        order,
        account,
        instrument,
        command,
        fill_volume: int,
        fill_price: Decimal,
        remaining_before: int,
        traded_before: int,
        average_before: Decimal,
        position,
        trade_id: str,
        position_id: str,
        position_detail_id: str,
        now,
        fee_calculator,
        trade_repository,
        position_repository,
    ) -> Trade:
        if (
            order.direction == OrderDirection.SELL.value
            and getattr(account, "risk_state", AccountRiskState.NORMAL.value)
            != AccountRiskState.NORMAL.value
        ):
            raise DataAccessError(
                "账户风险状态不允许继续成交卖出开仓期权",
                error_code="OPTION_RISK_INCREASE_NOT_ALLOWED",
            )
        allocated_margin = self._allocate(
            order.frozen_margin,
            fill_volume=fill_volume,
            remaining_volume=remaining_before,
        )
        released_cash = self._allocate(
            order.frozen_cash,
            fill_volume=fill_volume,
            remaining_volume=remaining_before,
        )
        released_commission = self._allocate(
            order.frozen_commission,
            fill_volume=fill_volume,
            remaining_volume=remaining_before,
        )
        actual_commission = fee_calculator.calculate_from_snapshot(
            price=fill_price,
            volume=fill_volume,
            commission_type=order.commission_type,
            commission_parameter=order.commission_parameter,
            contract_multiplier=order.commission_contract_multiplier,
        )
        order_multiplier = Decimal(order.commission_contract_multiplier)
        if order_multiplier <= 0:
            raise DataAccessError(
                "期权订单合约乘数快照不合法",
                error_code="OPTION_ORDER_MULTIPLIER_INVALID",
            )
        premium = quantize_money(
            fill_price
            * Decimal(fill_volume)
            * order_multiplier
        )
        premium_cash_flow = (
            -premium
            if order.direction == OrderDirection.BUY.value
            else premium
        )
        if (
            account.frozen_margin < allocated_margin
            or account.frozen_cash < released_cash
            or account.frozen_commission < released_commission
        ):
            raise DataAccessError(
                "期权开仓成交冻结资源不一致",
                error_code="OPTION_OPEN_RESOURCE_INCONSISTENT",
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
            instrument_type=order.instrument_type,
            direction=order.direction,
            offset_flag=order.offset_flag,
            trade_price=fill_price,
            trade_volume=fill_volume,
            turnover=premium,
            margin=allocated_margin,
            premium_cash_flow=premium_cash_flow,
            margin_rule_id=order.margin_rule_id,
            margin_rule_version=order.margin_rule_version,
            margin_calculation_version=order.margin_calculation_version,
            commission=actual_commission,
            realized_pnl=Decimal("0.000000"),
            daily_close_pnl=Decimal("0.000000"),
            trade_time=command.tick_event_time,
            created_at=now,
        )
        trade_repository.add(db, trade)

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
        order.frozen_cash = quantize_money(order.frozen_cash - released_cash)
        order.frozen_commission = quantize_money(
            order.frozen_commission - released_commission
        )
        order.updated_at = now

        account.frozen_margin = quantize_money(
            account.frozen_margin - allocated_margin
        )
        account.frozen_cash = quantize_money(
            account.frozen_cash - released_cash
        )
        account.frozen_commission = quantize_money(
            account.frozen_commission - released_commission
        )
        account.used_margin = quantize_money(
            account.used_margin + allocated_margin
        )
        account.option_used_margin = quantize_money(
            account.option_used_margin + allocated_margin
        )
        # 成交后的账面保证金在下一轮实时行情重估前就是当前可靠风险要求。
        # 同步更新账户和持仓快照，确保用户可以立即平仓，不依赖未来Tick
        # 才修复realtime_required_margin。
        account.option_realtime_required_margin = quantize_money(
            account.option_realtime_required_margin + allocated_margin
        )
        account.used_commission = quantize_money(
            account.used_commission + actual_commission
        )
        account.daily_commission = quantize_money(
            account.daily_commission + actual_commission
        )
        account.cash_balance = quantize_money(
            account.cash_balance + premium_cash_flow - actual_commission
        )
        # 成交时以成交权利金同步建立初始市值，避免等到下一轮行情估值前
        # 暂时高估或低估账户权益。
        if order.direction == OrderDirection.BUY.value:
            account.long_option_market_value = quantize_money(
                account.long_option_market_value + premium
            )
        else:
            account.short_option_market_value = quantize_money(
                account.short_option_market_value + premium
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
        account.equity = valuation.equity
        account.available_cash = valuation.available_cash
        account.risk_available_cash = valuation.risk_available_cash
        account.net_option_market_value = valuation.net_option_market_value
        account.daily_pnl = quantize_money(
            account.daily_position_pnl
            + account.daily_close_pnl
            - account.daily_commission
        )
        account.updated_at = now

        position_direction = (
            PositionDirection.LONG.value
            if order.direction == OrderDirection.BUY.value
            else PositionDirection.SHORT.value
        )
        if position is None:
            position = Position(
                position_id=position_id,
                account_id=order.account_id,
                order_book_id=order.order_book_id,
                exchange_id=order.exchange_id,
                symbol=order.symbol,
                instrument_type=order.instrument_type,
                direction=position_direction,
                total_volume=0,
                today_volume=0,
                yesterday_volume=0,
                frozen_volume=0,
                settlement_locked_volume=0,
                available_volume=0,
                average_open_price=Decimal("0"),
                position_cost=Decimal("0"),
                used_margin=Decimal("0"),
                initial_occupied_margin=Decimal("0"),
                realtime_required_margin=Decimal("0"),
                option_market_value=Decimal("0"),
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                daily_position_pnl=Decimal("0"),
                daily_close_pnl=Decimal("0"),
                trading_day=order.trading_day,
                multiplier_snapshot=order_multiplier,
                created_at=now,
                updated_at=now,
            )
            position_repository.add(db, position)
        old_volume = position.total_volume
        if old_volume > 0 and (
            position.margin_rule_id != order.margin_rule_id
            or position.margin_rule_version != order.margin_rule_version
            or (position.margin_rule_snapshot or {})
            != (order.margin_rule_snapshot or {})
        ):
            raise DataAccessError(
                "期权订单与既有持仓保证金规则快照不一致",
                error_code="OPTION_POSITION_RULE_SNAPSHOT_INCONSISTENT",
            )
        new_volume = old_volume + fill_volume
        position.average_open_price = quantize_money(
            (
                position.average_open_price * Decimal(old_volume)
                + fill_price * Decimal(fill_volume)
            )
            / Decimal(new_volume)
        )
        position.total_volume = new_volume
        position.today_volume += fill_volume
        position.available_volume = (
            position.total_volume
            - position.frozen_volume
            - position.settlement_locked_volume
        )
        position.position_cost = quantize_money(
            position.position_cost + premium
        )
        position.option_market_value = quantize_money(
            position.option_market_value + premium
        )
        position.used_margin = quantize_money(
            position.used_margin + allocated_margin
        )
        position.realtime_required_margin = quantize_money(
            position.realtime_required_margin + allocated_margin
        )
        position.initial_occupied_margin = quantize_money(
            position.initial_occupied_margin + allocated_margin
        )
        position.margin_rule_id = order.margin_rule_id
        position.margin_rule_version = order.margin_rule_version
        position.margin_rule_snapshot = order.margin_rule_snapshot
        position.margin_price_mode = order.margin_price_mode
        position.margin_underlying_price = order.margin_underlying_price
        position.margin_option_price = fill_price
        position.margin_calculated_at = now
        if Decimal(position.multiplier_snapshot) != order_multiplier:
            raise DataAccessError(
                "期权订单与既有持仓乘数快照不一致",
                error_code="OPTION_POSITION_MULTIPLIER_INCONSISTENT",
            )
        position.updated_at = now

        detail = PositionDetail(
            position_detail_id=position_detail_id,
            position_id=position.position_id,
            account_id=order.account_id,
            open_trade_id=trade.trade_id,
            order_book_id=order.order_book_id,
            exchange_id=order.exchange_id,
            symbol=order.symbol,
            instrument_type=order.instrument_type,
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
            realtime_required_margin=allocated_margin,
            margin_rule_id=order.margin_rule_id,
            margin_rule_version=order.margin_rule_version,
            margin_rule_snapshot=order.margin_rule_snapshot,
            margin_price_mode=order.margin_price_mode,
            margin_underlying_price=order.margin_underlying_price,
            margin_option_price=fill_price,
            margin_calculated_at=now,
            multiplier_snapshot=order_multiplier,
            open_commission=actual_commission,
            status=PositionDetailStatus.OPEN.value,
            created_at=now,
            updated_at=now,
        )
        position_repository.add_detail(db, detail)
        return trade
