from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.common.decimal_utils import quantize_money
from app.common.exceptions import DataAccessError
from app.enums.option_enums import InstrumentType, OptionType
from app.enums.order_enums import OffsetFlag, PositionDirection


ZERO = Decimal("0.000000")
OPTION_TYPES = {
    InstrumentType.FUTURES_OPTION.value,
    InstrumentType.INDEX_OPTION.value,
}


@dataclass(frozen=True)
class ReplayedDetail:
    position_detail_id: str
    position_id: str
    opening_volume: int
    today_open_volume: int
    close_today_volume: int
    close_yesterday_volume: int
    ending_volume_before_expiry: int
    ending_volume: int
    previous_basis: Decimal
    close_pnl: Decimal
    holding_pnl: Decimal
    cumulative_economic_pnl: Decimal


@dataclass(frozen=True)
class ReplayedPosition:
    position_id: str
    account_id: str
    order_book_id: str
    exchange_id: str
    symbol: str
    instrument_type: str
    direction: str
    multiplier: Decimal
    opening_yesterday_volume: int
    today_open_volume: int
    today_close_today_volume: int
    today_close_yesterday_volume: int
    ending_volume_before_expiry: int
    ending_volume: int
    previous_basis: Decimal | None
    settlement_price: Decimal
    holding_pnl: Decimal
    close_pnl: Decimal
    option_economic_pnl: Decimal
    commission: Decimal
    premium_cash_flow: Decimal
    cumulative_economic_pnl: Decimal
    cumulative_realized_pnl: Decimal
    expiry_intrinsic_value: Decimal
    expiry_cash_flow: Decimal
    expiry_realized_pnl: Decimal
    expired_closed: bool
    details: tuple[ReplayedDetail, ...]

    @property
    def today_close_volume(self) -> int:
        return self.today_close_today_volume + self.today_close_yesterday_volume


@dataclass(frozen=True)
class SettlementReplayResult:
    positions: tuple[ReplayedPosition, ...]
    trades_by_account: Mapping[str, tuple[Any, ...]]


class SettlementReplayService:
    """只使用不可变开仓/成交/分配/昨结事实重建目标交易日状态。"""

    @staticmethod
    def _sign(direction: str) -> Decimal:
        if direction == PositionDirection.LONG.value:
            return Decimal("1")
        if direction == PositionDirection.SHORT.value:
            return Decimal("-1")
        raise DataAccessError("持仓方向不合法", error_code="REPLAY_DIRECTION_INVALID")

    @staticmethod
    def _terminal_price(instrument: Any, underlying_price: Decimal) -> Decimal:
        strike = Decimal(instrument.strike_price)
        if instrument.option_type == OptionType.CALL.value:
            return quantize_money(max(underlying_price - strike, ZERO))
        if instrument.option_type == OptionType.PUT.value:
            return quantize_money(max(strike - underlying_price, ZERO))
        raise DataAccessError(
            "到期期权类型不合法",
            error_code="REPLAY_OPTION_TYPE_INVALID",
        )

    def replay(
        self,
        *,
        trading_day: date,
        details: Sequence[Any],
        trades: Sequence[Any],
        allocations: Sequence[Any],
        prior_position_settlements: Mapping[str, Any],
        prior_expired_position_ids: set[str],
        instruments: Mapping[str, Any],
        instruments_by_id: Mapping[int, Any],
        prices: Mapping[tuple[str, str], Decimal],
        has_prior_batch: bool,
    ) -> SettlementReplayResult:
        trade_by_id = {item.trade_id: item for item in trades}
        if len(trade_by_id) != len(trades):
            raise DataAccessError(
                "成交编号不唯一",
                error_code="REPLAY_TRADE_DUPLICATE",
            )
        allocations_by_detail: dict[str, list[Any]] = defaultdict(list)
        allocations_by_trade: dict[str, list[Any]] = defaultdict(list)
        for item in allocations:
            allocations_by_detail[item.position_detail_id].append(item)
            allocations_by_trade[item.trade_id].append(item)

        detail_by_open_trade: dict[str, Any] = {}
        details_by_position: dict[str, list[Any]] = defaultdict(list)
        for detail in details:
            if detail.open_trade_id in detail_by_open_trade:
                raise DataAccessError(
                    "一笔开仓成交对应多条持仓明细",
                    error_code="REPLAY_OPEN_DETAIL_DUPLICATE",
                )
            detail_by_open_trade[detail.open_trade_id] = detail
            details_by_position[detail.position_id].append(detail)
            trade = trade_by_id.get(detail.open_trade_id)
            if (
                trade is None
                or trade.offset_flag != OffsetFlag.OPEN.value
                or trade.trade_volume != detail.original_volume
                or trade.account_id != detail.account_id
                or trade.order_book_id != detail.order_book_id
                or trade.trading_day != detail.open_trading_day
                or Decimal(trade.trade_price) != Decimal(detail.open_price)
            ):
                raise DataAccessError(
                    "开仓持仓明细与不可变成交不一致",
                    error_code="REPLAY_OPEN_TRADE_INCONSISTENT",
                )

        for trade in trades:
            if trade.offset_flag == OffsetFlag.OPEN.value:
                if trade.trade_id not in detail_by_open_trade:
                    raise DataAccessError(
                        "开仓成交缺少持仓明细",
                        error_code="REPLAY_OPEN_DETAIL_MISSING",
                    )
                continue
            rows = allocations_by_trade.get(trade.trade_id, [])
            if sum(item.close_volume for item in rows) != trade.trade_volume:
                raise DataAccessError(
                    "平仓成交与持仓分配数量不一致",
                    error_code="REPLAY_CLOSE_ALLOCATION_INCONSISTENT",
                )
            if quantize_money(
                sum((Decimal(item.commission) for item in rows), ZERO)
            ) != quantize_money(Decimal(trade.commission)):
                raise DataAccessError(
                    "平仓成交与持仓分配手续费不一致",
                    error_code="REPLAY_CLOSE_COMMISSION_INCONSISTENT",
                )

        daily_trades = [item for item in trades if item.trading_day == trading_day]
        trades_by_account: dict[str, list[Any]] = defaultdict(list)
        commission_by_position: dict[str, Decimal] = defaultdict(lambda: ZERO)
        premium_by_position: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for trade in daily_trades:
            trades_by_account[trade.account_id].append(trade)
            if trade.offset_flag == OffsetFlag.OPEN.value:
                position_id = detail_by_open_trade[trade.trade_id].position_id
                target_positions = {position_id}
            else:
                target_positions = {
                    item.position_id for item in allocations_by_trade[trade.trade_id]
                }
            if len(target_positions) != 1:
                raise DataAccessError(
                    "一笔成交跨越多个汇总持仓",
                    error_code="REPLAY_TRADE_POSITION_AMBIGUOUS",
                )
            position_id = next(iter(target_positions))
            commission_by_position[position_id] = quantize_money(
                commission_by_position[position_id] + Decimal(trade.commission)
            )
            premium_by_position[position_id] = quantize_money(
                premium_by_position[position_id]
                + Decimal(getattr(trade, "premium_cash_flow", ZERO))
            )

        replayed_positions: list[ReplayedPosition] = []
        for position_id, position_details in sorted(details_by_position.items()):
            first = position_details[0]
            if any(
                item.account_id != first.account_id
                or item.order_book_id != first.order_book_id
                or item.direction != first.direction
                or Decimal(item.multiplier_snapshot)
                != Decimal(first.multiplier_snapshot)
                for item in position_details
            ):
                raise DataAccessError(
                    "同一汇总持仓的不可变开仓明细不一致",
                    error_code="REPLAY_DETAIL_IDENTITY_INCONSISTENT",
                )
            instrument = instruments.get(first.order_book_id)
            if instrument is None:
                raise DataAccessError(
                    "回放成交对应合约不存在",
                    error_code="REPLAY_INSTRUMENT_MISSING",
                )
            multiplier = Decimal(first.multiplier_snapshot)
            if multiplier <= ZERO:
                raise DataAccessError(
                    "回放成交乘数不合法",
                    error_code="REPLAY_MULTIPLIER_INVALID",
                )
            sign = self._sign(first.direction)
            settlement_price = prices.get(
                (instrument.exchange_id, instrument.symbol)
            )

            is_option = instrument.instrument_type in OPTION_TYPES
            expires_today = bool(
                is_option
                and instrument.expire_date is not None
                and instrument.expire_date == trading_day
            )
            terminal_price = settlement_price
            expiry_intrinsic = ZERO
            if expires_today:
                underlying = instruments_by_id.get(
                    instrument.underlying_instrument_id or -1
                )
                if underlying is None:
                    raise DataAccessError(
                        "到期期权标的不存在",
                        error_code="REPLAY_UNDERLYING_MISSING",
                    )
                underlying_price = prices.get(
                    (underlying.exchange_id, underlying.symbol)
                )
                if underlying_price is None:
                    raise DataAccessError(
                        "到期期权标的结算价不存在",
                        error_code="REPLAY_UNDERLYING_PRICE_MISSING",
                    )
                terminal_price = self._terminal_price(
                    instrument, underlying_price
                )
                expiry_intrinsic = terminal_price

            prior = prior_position_settlements.get(position_id)
            detail_results: list[ReplayedDetail] = []
            opening_total = today_open_total = 0
            close_today_total = close_yesterday_total = 0
            ending_before_expiry = 0
            holding_pnl = close_pnl = cumulative = ZERO
            cumulative_realized = ZERO
            for detail in sorted(position_details, key=lambda item: item.id):
                rows = allocations_by_detail.get(detail.position_detail_id, [])
                if any(
                    item.position_id != position_id
                    or item.open_trading_day != detail.open_trading_day
                    or Decimal(item.open_price) != Decimal(detail.open_price)
                    for item in rows
                ):
                    raise DataAccessError(
                        "平仓分配与开仓明细不一致",
                        error_code="REPLAY_ALLOCATION_DETAIL_INCONSISTENT",
                    )
                prior_closed = sum(
                    item.close_volume
                    for item in rows
                    if item.close_trading_day < trading_day
                )
                today_rows = [
                    item for item in rows if item.close_trading_day == trading_day
                ]
                future_closed = sum(
                    item.close_volume
                    for item in rows
                    if item.close_trading_day > trading_day
                )
                if future_closed:
                    raise DataAccessError(
                        "数据库存在目标日之后的平仓分配，禁止倒序重放",
                        error_code="REPLAY_FUTURE_ALLOCATION_EXISTS",
                    )
                opened_today = (
                    detail.original_volume
                    if detail.open_trading_day == trading_day
                    else 0
                )
                opening = (
                    detail.original_volume - prior_closed
                    if detail.open_trading_day < trading_day
                    else 0
                )
                if position_id in prior_expired_position_ids:
                    opening = 0
                close_today = sum(
                    item.close_volume
                    for item in today_rows
                    if item.resolved_offset_flag == OffsetFlag.CLOSE_TODAY.value
                )
                close_yesterday = sum(
                    item.close_volume
                    for item in today_rows
                    if item.resolved_offset_flag
                    == OffsetFlag.CLOSE_YESTERDAY.value
                )
                if close_today + close_yesterday != sum(
                    item.close_volume for item in today_rows
                ):
                    raise DataAccessError(
                        "平仓分配缺少明确平今/平昨标记",
                        error_code="REPLAY_CLOSE_OFFSET_INVALID",
                    )
                if close_today and detail.open_trading_day != trading_day:
                    raise DataAccessError(
                        "平今分配引用的不是今日开仓明细",
                        error_code="REPLAY_CLOSE_TODAY_INVALID",
                    )
                if close_yesterday and detail.open_trading_day >= trading_day:
                    raise DataAccessError(
                        "平昨分配引用的不是历史开仓明细",
                        error_code="REPLAY_CLOSE_YESTERDAY_INVALID",
                    )
                ending = opening + opened_today - close_today - close_yesterday
                if opening < 0 or ending < 0:
                    raise DataAccessError(
                        "成交回放后持仓数量为负",
                        error_code="REPLAY_POSITION_VOLUME_NEGATIVE",
                    )
                if (
                    instrument.expire_date is not None
                    and instrument.expire_date < trading_day
                    and ending > 0
                ):
                    raise DataAccessError(
                        "历史已到期期权仍有活动数量",
                        error_code="HISTORICAL_EXPIRED_POSITION_ACTIVE",
                    )
                if ending > 0 and terminal_price is None:
                    raise DataAccessError(
                        "日终剩余持仓缺少结算价",
                        error_code="REPLAY_SETTLEMENT_PRICE_MISSING",
                    )
                if detail.open_trading_day < trading_day:
                    if prior is not None:
                        basis = Decimal(prior.settlement_price)
                    elif has_prior_batch:
                        raise DataAccessError(
                            "历史持仓缺少上一交易日结算事实",
                            error_code="PRIOR_POSITION_SETTLEMENT_MISSING",
                        )
                    else:
                        basis = Decimal(detail.open_price)
                else:
                    basis = Decimal(detail.open_price)
                detail_close_pnl = quantize_money(
                    sum(
                        (
                            (
                                Decimal(item.close_price) - basis
                            )
                            * multiplier
                            * Decimal(item.close_volume)
                            * sign
                            for item in today_rows
                        ),
                        ZERO,
                    )
                )
                detail_holding_pnl = quantize_money(
                    (Decimal(terminal_price) - basis)
                    * multiplier
                    * Decimal(ending)
                    * sign
                    if ending > 0
                    else ZERO
                )
                detail_cumulative = quantize_money(
                    (Decimal(terminal_price) - Decimal(detail.open_price))
                    * multiplier
                    * Decimal(ending)
                    * sign
                    if ending > 0 and not expires_today
                    else ZERO
                )
                opening_total += opening
                today_open_total += opened_today
                close_today_total += close_today
                close_yesterday_total += close_yesterday
                ending_before_expiry += ending
                close_pnl = quantize_money(close_pnl + detail_close_pnl)
                holding_pnl = quantize_money(holding_pnl + detail_holding_pnl)
                cumulative = quantize_money(cumulative + detail_cumulative)
                cumulative_realized = quantize_money(
                    cumulative_realized
                    + sum(
                        (Decimal(item.realized_pnl) for item in rows),
                        ZERO,
                    )
                )
                detail_results.append(
                    ReplayedDetail(
                        detail.position_detail_id,
                        position_id,
                        opening,
                        opened_today,
                        close_today,
                        close_yesterday,
                        ending,
                        0 if expires_today else ending,
                        quantize_money(basis),
                        detail_close_pnl,
                        detail_holding_pnl,
                        detail_cumulative,
                    )
                )

            close_total = close_today_total + close_yesterday_total
            if opening_total + today_open_total - close_total != ending_before_expiry:
                raise DataAccessError(
                    "期初持仓加开仓减平仓不等于期末持仓",
                    error_code="REPLAY_POSITION_CONSERVATION_FAILED",
                )
            in_scope = bool(opening_total or today_open_total or close_total)
            if not in_scope:
                continue
            if settlement_price is None:
                raise DataAccessError(
                    "当日审计持仓缺少冻结结算价",
                    error_code="REPLAY_AUDIT_PRICE_MISSING",
                )
            expired_closed = expires_today and ending_before_expiry > 0
            expiry_cash = (
                quantize_money(
                    expiry_intrinsic
                    * multiplier
                    * Decimal(ending_before_expiry)
                    * sign
                )
                if expired_closed
                else ZERO
            )
            expiry_realized = (
                quantize_money(
                    sum(
                        (
                            expiry_intrinsic - Decimal(item.open_price)
                        )
                        * multiplier
                        * Decimal(result.ending_volume_before_expiry)
                        * sign
                        for item, result in zip(
                            sorted(position_details, key=lambda value: value.id),
                            detail_results,
                            strict=True,
                        )
                    )
                )
                if expired_closed
                else ZERO
            )
            option_economic = (
                quantize_money(holding_pnl + close_pnl)
                if is_option
                else ZERO
            )
            prior_basis = (
                quantize_money(
                    sum(
                        item.previous_basis * Decimal(item.opening_volume)
                        for item in detail_results
                    )
                    / Decimal(opening_total)
                )
                if opening_total > 0
                else None
            )
            replayed_positions.append(
                ReplayedPosition(
                    position_id=position_id,
                    account_id=first.account_id,
                    order_book_id=first.order_book_id,
                    exchange_id=first.exchange_id,
                    symbol=first.symbol,
                    instrument_type=instrument.instrument_type,
                    direction=first.direction,
                    multiplier=multiplier,
                    opening_yesterday_volume=opening_total,
                    today_open_volume=today_open_total,
                    today_close_today_volume=close_today_total,
                    today_close_yesterday_volume=close_yesterday_total,
                    ending_volume_before_expiry=ending_before_expiry,
                    ending_volume=0 if expired_closed else ending_before_expiry,
                    previous_basis=prior_basis,
                    settlement_price=Decimal(settlement_price),
                    holding_pnl=holding_pnl,
                    close_pnl=close_pnl,
                    option_economic_pnl=option_economic,
                    commission=commission_by_position[position_id],
                    premium_cash_flow=premium_by_position[position_id],
                    cumulative_economic_pnl=(ZERO if expired_closed else cumulative),
                    cumulative_realized_pnl=quantize_money(
                        cumulative_realized + expiry_realized
                    ),
                    expiry_intrinsic_value=expiry_intrinsic,
                    expiry_cash_flow=expiry_cash,
                    expiry_realized_pnl=expiry_realized,
                    expired_closed=expired_closed,
                    details=tuple(detail_results),
                )
            )

        return SettlementReplayResult(
            positions=tuple(replayed_positions),
            trades_by_account={
                account_id: tuple(
                    sorted(items, key=lambda item: (item.trade_time, item.id))
                )
                for account_id, items in trades_by_account.items()
            },
        )
