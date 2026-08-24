"""日终结算数据核查（只读）。

检查今天(2026-08-21)结算批次、账户/持仓结算记录、资金守恒与关键字段。
"""
import json
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select, text

from app.core.database import SessionLocal
from app.models.daily_settlement import (
    DailyAccountSettlement,
    DailyPositionSettlement,
    DailySettlementBatch,
    InstrumentSettlementPrice,
    OptionExpirySettlementDetail,
)
from app.models.account import Account
from app.models.position import Position
from app.models.trade import Trade

DAY = date(2026, 8, 21)


def show(title, rows):
    print(f"\n===== {title} =====")
    for r in rows:
        print(json.dumps(r, ensure_ascii=False, default=str))


with SessionLocal() as db:
    # 1. 批次
    batches = [
        {
            "batch_id": b.batch_id,
            "trading_day": str(b.trading_day),
            "status": b.status,
            "current_stage": b.current_stage,
            "started_at": str(b.started_at),
            "completed_at": str(b.completed_at),
            "failed_at": str(b.failed_at),
            "failure_code": b.failure_code,
            "failure_message": (b.failure_message or "")[:200],
            "cache_status": b.cache_status,
        }
        for b in db.scalars(
            select(DailySettlementBatch).order_by(DailySettlementBatch.id.desc()).limit(10)
        ).all()
    ]
    show("结算批次（最近10个）", batches)

    # 2. 账户结算记录（今天）
    acc_settles = [
        {
            "account_id": s.account_id,
            "status": s.status,
            "cash_before": str(s.cash_balance_before),
            "cash_after": str(s.cash_balance_after),
            "futures_settlement_pnl": str(s.futures_settlement_pnl),
            "option_expiry_cash_flow": str(s.option_expiry_cash_flow),
            "trade_cash_flow": str(s.trade_cash_flow),
            "futures_close_pnl": str(s.futures_close_pnl),
            "option_economic_pnl": str(s.option_economic_pnl),
            "option_premium_cash_flow": str(s.option_premium_cash_flow),
            "daily_close_pnl": str(s.daily_close_pnl),
            "daily_net_pnl": str(s.daily_net_pnl),
            "daily_commission": str(s.daily_commission),
            "settled_at": str(s.settled_at),
            "failure_code": s.failure_code,
            "failure_message": (s.failure_message or "")[:120],
            "reconciliation": s.reconciliation_snapshot,
        }
        for s in db.scalars(
            select(DailyAccountSettlement)
            .where(DailyAccountSettlement.trading_day == DAY)
            .order_by(DailyAccountSettlement.id)
        ).all()
    ]
    show(f"账户结算记录 {DAY}（{len(acc_settles)}条）", acc_settles)

    # 3. 持仓结算记录（今天）
    pos_settles = [
        {
            "account_id": s.account_id,
            "position_id": s.position_id,
            "symbol": s.symbol,
            "direction": s.direction,
            "volume_before": s.volume_before,
            "today_open": s.today_open_volume,
            "today_close": s.today_close_volume,
            "today_vol_before": s.today_volume_before,
            "yday_vol_before": s.yesterday_volume_before,
            "volume_after": s.volume_after,
            "today_vol_after": s.today_volume_after,
            "yday_vol_after": s.yesterday_volume_after,
            "settlement_price": str(s.settlement_price),
            "daily_settlement_pnl": str(s.daily_settlement_pnl),
            "close_pnl": str(s.close_pnl),
            "commission": str(s.commission),
            "settlement_margin": str(s.settlement_margin),
            "option_market_value": str(s.option_market_value),
            "expired_closed": s.expired_closed,
            "settled_at": str(s.settled_at),
        }
        for s in db.scalars(
            select(DailyPositionSettlement)
            .where(DailyPositionSettlement.trading_day == DAY)
            .order_by(DailyPositionSettlement.account_id, DailyPositionSettlement.id)
        ).all()
    ]
    show(f"持仓结算记录 {DAY}（{len(pos_settles)}条）", pos_settles)

    # 4. 期权到期结算明细
    expiries = [
        {
            "account_id": e.account_id,
            "position_id": e.position_id,
            "option_order_book_id": e.option_order_book_id,
            "direction": e.direction,
            "strike_price": str(e.strike_price),
            "quantity": e.quantity,
            "intrinsic_value": str(e.intrinsic_value),
            "gross_cash_amount": str(e.gross_cash_amount),
            "cash_flow": str(e.cash_flow),
            "realized_pnl": str(e.realized_pnl),
        }
        for e in db.scalars(
            select(OptionExpirySettlementDetail)
            .where(OptionExpirySettlementDetail.trading_day == DAY)
        ).all()
    ]
    show(f"期权到期结算明细 {DAY}", expiries)

    # 5. 结算价冻结
    prices = [
        {
            "exchange_id": p.exchange_id,
            "symbol": p.symbol,
            "settlement_price": str(p.settlement_price),
            "price_source": p.price_source,
            "source_tick_time": str(p.source_tick_time),
            "source_tick_trading_day": str(p.source_tick_trading_day),
        }
        for p in db.scalars(
            select(InstrumentSettlementPrice)
            .where(InstrumentSettlementPrice.trading_day == DAY)
            .order_by(InstrumentSettlementPrice.exchange_id, InstrumentSettlementPrice.symbol)
        ).all()
    ]
    show(f"结算价冻结 {DAY}（{len(prices)}条）", prices)

    # 6. 账户当前状态
    accounts = [
        {
            "account_id": a.account_id,
            "user_id": a.user_id,
            "trading_day": str(a.trading_day),
            "cash_balance": str(a.cash_balance),
            "available_cash": str(a.available_cash),
            "used_margin": str(a.used_margin),
            "frozen_margin": str(a.frozen_margin),
            "frozen_cash": str(a.frozen_cash),
            "frozen_commission": str(a.frozen_commission),
            "option_used_margin": str(a.option_used_margin),
            "realized_pnl": str(a.realized_pnl),
            "unrealized_pnl": str(a.unrealized_pnl),
            "daily_position_pnl": str(a.daily_position_pnl),
            "daily_close_pnl": str(a.daily_close_pnl),
            "daily_commission": str(a.daily_commission),
            "daily_pnl": str(a.daily_pnl),
            "used_commission": str(a.used_commission),
            "cumulative_net_pnl": str(getattr(a, "cumulative_net_pnl", None)),
            "updated_at": str(a.updated_at),
        }
        for a in db.scalars(
            select(Account).order_by(Account.user_id, Account.account_id)
        ).all()
    ]
    show(f"账户当前状态（{len(accounts)}个）", accounts)

    # 7. 持仓当前状态
    positions = [
        {
            "position_id": p.position_id,
            "account_id": p.account_id,
            "symbol": p.symbol,
            "direction": p.direction,
            "total_volume": p.total_volume,
            "today_volume": p.today_volume,
            "yesterday_volume": p.yesterday_volume,
            "frozen_volume": p.frozen_volume,
            "available_volume": p.available_volume,
            "average_open_price": str(p.average_open_price),
            "settlement_price": str(getattr(p, "settlement_price", None)),
            "unrealized_pnl": str(p.unrealized_pnl),
            "used_margin": str(p.used_margin),
            "trading_day": str(p.trading_day),
            "updated_at": str(p.updated_at),
        }
        for p in db.scalars(
            select(Position).where(Position.total_volume > 0).order_by(Position.id)
        ).all()
    ]
    show(f"持仓当前状态（{len(positions)}条）", positions)

    # 8. 今日成交与手续费（用于核对）
    trades = db.execute(
        text(
            f"""
            SELECT account_id, count(*) AS cnt,
                   sum(close_pnl) AS sum_close_pnl,
                   sum(commission) AS sum_commission,
                   sum(fee_amount) AS sum_fee
            FROM trades WHERE trading_day = :day
            GROUP BY account_id ORDER BY account_id
            """
        ),
        {"day": DAY},
    ).mappings().all()
    show(f"今日成交汇总 {DAY}", [{k: str(v) for k, v in r.items()} for r in trades])

    # 9. 今日成交明细（前30条）
    trade_rows = [
        {
            "trade_id": t.trade_id,
            "account_id": t.account_id,
            "symbol": t.symbol,
            "direction": t.direction,
            "offset_flag": t.offset_flag,
            "volume": t.volume,
            "price": str(t.price),
            "commission": str(getattr(t, "commission", None)),
            "close_pnl": str(getattr(t, "close_pnl", None)),
            "trading_day": str(t.trading_day),
            "matched_at": str(getattr(t, "matched_at", None)),
        }
        for t in db.scalars(
            select(Trade).where(Trade.trading_day == DAY).order_by(Trade.id)
        ).all()
    ]
    show(f"今日成交明细（{len(trade_rows)}条）", trade_rows[:30])
