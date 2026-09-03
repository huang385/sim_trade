"""Deterministic replay of the cash-security aggregate holding layer.

Cash securities deliberately do not use derivative ``PositionDetail`` lots.
Their auditable holding source is the immutable cash Trade stream together
with corporate-action position-adjustment facts.  This module is the one
place that turns those facts into the mutable ``Position`` projection used by
orders, settlement and valuation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable

from app.common.decimal_utils import quantize_money
from app.common.exceptions import DataAccessError


ZERO = Decimal("0")


@dataclass
class CashSecurityPositionReplayProjection:
    """Replay result for one aggregate cash-security position."""

    total_volume: int = 0
    today_volume: int = 0
    yesterday_volume: int = 0
    pending_share_volume: int = 0
    frozen_volume: int = 0
    settlement_locked_volume: int = 0
    available_volume: int = 0
    position_cost: Decimal = ZERO
    daily_pnl_base_cost: Decimal = ZERO
    yesterday_pnl_base_cost: Decimal = ZERO
    today_pnl_base_cost: Decimal = ZERO
    daily_pnl_base_established: bool = False
    average_open_price: Decimal = ZERO
    authoritative: bool = False

    @classmethod
    def replay(
        cls,
        *,
        position,
        trades: Iterable,
        adjustments: Iterable,
        trading_day: date,
        market_tplus: int | None = None,
    ) -> "CashSecurityPositionReplayProjection":
        """Build a projection without reading mutable Position quantities.

        A legacy opening-balance adjustment is accepted only for positions
        that have no cash Trade history.  It is an explicit immutable fact,
        not a fallback to today's Position aggregate.
        """

        rows = [item for item in adjustments if item.effective_trading_day <= trading_day]
        cash_trades = [
            item
            for item in trades
            if item.account_id == position.account_id
            and item.exchange_id == position.exchange_id
            and item.symbol == position.symbol
            and item.instrument_type == position.instrument_type
            and item.trading_day <= trading_day
        ]
        if not rows and not cash_trades:
            # Pre-cash-security legacy positions have no provable source yet.
            # Keep them readable until a migration/admin opening fact exists;
            # never silently turn their current aggregate into a replay input.
            return cls(authoritative=False)

        result = cls(authoritative=True)
        events: dict[date, tuple[list, list]] = {}
        for trade in cash_trades:
            events.setdefault(trade.trading_day, ([], []))[0].append(trade)
        for adjustment in rows:
            events.setdefault(adjustment.effective_trading_day, ([], []))[1].append(
                adjustment
            )

        prior_day: date | None = None
        for day in sorted(events):
            if prior_day is not None and day != prior_day:
                result.yesterday_volume += result.today_volume
                result.today_volume = 0
                result.settlement_locked_volume = 0
                result.today_pnl_base_cost = ZERO
                result.daily_pnl_base_cost = result.yesterday_pnl_base_cost
            day_trades, day_adjustments = events[day]
            for trade in sorted(day_trades, key=lambda item: (item.trade_time, item.id)):
                result._apply_trade(
                    trade,
                    instrument_type=position.instrument_type,
                    market_tplus=market_tplus,
                )
            for adjustment in sorted(
                day_adjustments,
                key=lambda item: (
                    item.effective_trading_day,
                    item.action_id,
                    item.action_version,
                    item.component_id,
                    item.id,
                ),
            ):
                result._apply_adjustment(adjustment)
            result._validate()
            prior_day = day
        return result

    def _apply_trade(
        self, trade, *, instrument_type: str, market_tplus: int | None
    ) -> None:
        from app.services.cash_security_position_service import (
            CashSecurityPositionService,
        )

        settlement_days = CashSecurityPositionService.settlement_days(
            instrument_type=instrument_type, market_tplus=market_tplus
        )
        volume = int(trade.trade_volume)
        if trade.direction == "BUY":
            self.total_volume += volume
            self.today_volume += volume
            if settlement_days == 1:
                self.settlement_locked_volume += volume
            else:
                self.available_volume += volume
            turnover = Decimal(trade.turnover)
            self.position_cost = quantize_money(self.position_cost + turnover)
            self.today_pnl_base_cost = quantize_money(
                self.today_pnl_base_cost + turnover
            )
        elif trade.direction == "SELL":
            if volume > self.total_volume:
                raise DataAccessError(
                    "现金证券重放卖出数量超过可重放持仓",
                    error_code="CASH_SECURITY_REPLAY_SELL_VOLUME_INVALID",
                )
            if settlement_days == 1:
                if volume > self.yesterday_volume:
                    raise DataAccessError(
                        "现金证券重放卖出违反T+1",
                        error_code=(
                            "CASH_SECURITY_REPLAY_STOCK_T_PLUS_ONE"
                            if instrument_type == "STOCK"
                            else "CASH_SECURITY_REPLAY_ETF_T_PLUS_ONE"
                        ),
                    )
                yesterday_sold, today_sold = volume, 0
            else:
                yesterday_sold = min(self.yesterday_volume, volume)
                today_sold = volume - yesterday_sold
                if today_sold > self.today_volume:
                    raise DataAccessError(
                        "T+0现金证券重放卖出数量无来源",
                        error_code="CASH_SECURITY_REPLAY_T_ZERO_SELL_VOLUME_INVALID",
                    )
            total_before = self.total_volume
            cost = (
                self.position_cost
                if volume == total_before
                else quantize_money(self.position_cost * Decimal(volume) / Decimal(total_before))
            )
            self.total_volume -= volume
            self.yesterday_volume -= yesterday_sold
            self.today_volume -= today_sold
            self.position_cost = quantize_money(self.position_cost - cost)
            self._reduce_bases(
                yesterday_sold=yesterday_sold,
                yesterday_before=self.yesterday_volume + yesterday_sold,
                today_sold=today_sold,
                today_before=self.today_volume + today_sold,
            )
        else:
            raise DataAccessError(
                "现金证券重放成交方向无效",
                error_code="CASH_SECURITY_REPLAY_TRADE_DIRECTION_INVALID",
            )
        self._refresh_available_and_average()

    def _reduce_bases(
        self, *, yesterday_sold: int, yesterday_before: int, today_sold: int, today_before: int
    ) -> None:
        if yesterday_sold:
            reduction = (
                self.yesterday_pnl_base_cost
                if yesterday_sold == yesterday_before
                else quantize_money(
                    self.yesterday_pnl_base_cost
                    * Decimal(yesterday_sold)
                    / Decimal(yesterday_before)
                )
            )
            self.yesterday_pnl_base_cost = quantize_money(
                self.yesterday_pnl_base_cost - reduction
            )
        if today_sold:
            reduction = (
                self.today_pnl_base_cost
                if today_sold == today_before
                else quantize_money(
                    self.today_pnl_base_cost * Decimal(today_sold) / Decimal(today_before)
                )
            )
            self.today_pnl_base_cost = quantize_money(
                self.today_pnl_base_cost - reduction
            )
        self.daily_pnl_base_cost = quantize_money(
            self.yesterday_pnl_base_cost + self.today_pnl_base_cost
        )

    def _apply_adjustment(self, row) -> None:
        if row.business_version != str(row.action_version):
            raise DataAccessError(
                "公司行为调整事实版本不一致",
                error_code="CORPORATE_ACTION_ADJUSTMENT_VERSION_INVALID",
            )
        self.total_volume += int(row.total_volume_delta)
        self.today_volume += int(row.today_volume_delta)
        self.yesterday_volume += int(row.yesterday_volume_delta)
        self.pending_share_volume += int(row.pending_volume_delta)
        self.frozen_volume += int(row.frozen_volume_delta)
        self.settlement_locked_volume += int(row.settlement_locked_volume_delta)
        self.available_volume += int(row.available_volume_delta)
        self.position_cost = quantize_money(
            self.position_cost + Decimal(row.position_cost_delta)
        )
        self.daily_pnl_base_cost = quantize_money(
            self.daily_pnl_base_cost + Decimal(row.daily_pnl_base_cost_delta)
        )
        # The explicit opening balance stores all buckets as deltas from zero.
        payload = row.replay_payload or {}
        if row.adjustment_type == "REPLAY_OPENING_BALANCE":
            self.yesterday_pnl_base_cost = Decimal(
                payload.get("yesterday_pnl_base_cost", "0")
            )
            self.today_pnl_base_cost = Decimal(payload.get("today_pnl_base_cost", "0"))
            self.daily_pnl_base_established = bool(
                payload.get("daily_pnl_base_established", False)
            )
        else:
            # Corporate-action deltas affect carried basis unless an explicit
            # future bucket is supplied in the immutable payload.
            self.yesterday_pnl_base_cost = quantize_money(
                self.yesterday_pnl_base_cost + Decimal(row.daily_pnl_base_cost_delta)
            )
        has_explicit_average = row.average_open_price_after is not None
        if has_explicit_average:
            self.average_open_price = Decimal(row.average_open_price_after)
        self._refresh_available_and_average(
            preserve_explicit_average=has_explicit_average
        )

    def _refresh_available_and_average(self, *, preserve_explicit_average: bool = False) -> None:
        self.available_volume = (
            self.total_volume - self.frozen_volume - self.settlement_locked_volume
        )
        if self.total_volume == 0:
            self.average_open_price = ZERO
        elif not preserve_explicit_average:
            self.average_open_price = quantize_money(
                self.position_cost / Decimal(self.total_volume)
            )
        self.daily_pnl_base_cost = quantize_money(
            self.yesterday_pnl_base_cost + self.today_pnl_base_cost
        )

    def _validate(self) -> None:
        if min(
            self.total_volume,
            self.today_volume,
            self.yesterday_volume,
            self.pending_share_volume,
            self.frozen_volume,
            self.settlement_locked_volume,
            self.available_volume,
        ) < 0 or self.total_volume != self.today_volume + self.yesterday_volume:
            raise DataAccessError(
                "现金证券公司行为重放持仓数量不守恒",
                error_code="CASH_SECURITY_REPLAY_POSITION_INVALID",
            )
        if self.available_volume != (
            self.total_volume - self.frozen_volume - self.settlement_locked_volume
        ):
            raise DataAccessError(
                "现金证券公司行为重放可用数量不守恒",
                error_code="CASH_SECURITY_REPLAY_AVAILABLE_INVALID",
            )
