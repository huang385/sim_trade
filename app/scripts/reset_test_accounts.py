"""仅重置指定 TEST_* 账户的交易事实，保留全局参考数据与 Redis 全局键。"""

from sqlalchemy import text

from app.core.database import SessionLocal
from app.core.redis_client import redis_client


ACCOUNT_IDS = (
    "TEST_ACC_20260812_01",
    "TEST_ACC_20260812_02",
    "TEST_ACC_20260812_03",
    "TEST_STOCK_20260812_01",
    "TEST_STOCK_20260812_02",
)


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values) or "''"


def main() -> None:
    accounts = _quoted(ACCOUNT_IDS)
    condition = f"account_id in ({accounts})"
    with SessionLocal() as db:
        try:
            order_ids = tuple(db.execute(text(f"select order_id from orders where {condition}")).scalars())
            trade_ids = tuple(db.execute(text(f"select trade_id from trade where {condition}")).scalars())
            position_ids = tuple(db.execute(text(f"select position_id from position where {condition}")).scalars())
            detail_ids = tuple(db.execute(text(f"select position_detail_id from position_detail where {condition}")).scalars())
            order_list, trade_list = _quoted(order_ids), _quoted(trade_ids)
            statements = (
                f"delete from cash_security_corporate_action_ledger where {condition}",
                f"delete from cash_security_corporate_action_position_adjustment where {condition}",
                f"delete from cash_security_corporate_action_subscription where {condition}",
                f"delete from cash_security_corporate_action_entitlement where {condition}",
                f"delete from cash_security_trade_fee_component where trade_id in ({trade_list})",
                f"delete from cash_security_order_fee_accumulator where order_id in ({order_list})",
                f"delete from trade_position_allocation where {condition}",
                f"delete from position_freeze_allocation where {condition}",
                f"delete from order_fee_component_snapshot where order_id in ({order_list})",
                f"delete from option_expiry_settlement_detail where {condition}",
                f"delete from daily_position_settlement where {condition}",
                f"delete from daily_account_settlement where {condition}",
                f"delete from liquidation_task where {condition}",
                f"delete from risk_event where {condition}",
                f"delete from trade where {condition}",
                f"delete from position_detail where {condition}",
                f"delete from position where {condition}",
                f"delete from orders where {condition}",
                f"delete from outbox_event where (payload ->> 'account_id') in ({accounts}) or aggregate_id in ({_quoted(ACCOUNT_IDS + order_ids + trade_ids + position_ids)})",
            )
            deleted = [db.execute(text(statement)).rowcount for statement in statements]
            reset = db.execute(text(f"""
                update account set cash_balance=initial_cash, available_cash=initial_cash,
                frozen_cash=0, equity=initial_cash, used_margin=0, frozen_margin=0,
                realized_pnl=0, unrealized_pnl=0, daily_pnl=0, used_commission=0,
                frozen_commission=0, risk_ratio=0, status='NORMAL', trading_day=null,
                daily_position_pnl=0, daily_close_pnl=0, daily_commission=0,
                option_used_margin=0, option_realtime_required_margin=0,
                long_option_market_value=0, short_option_market_value=0,
                net_option_market_value=0, risk_available_cash=initial_cash,
                risk_state='NORMAL', risk_version=0, cumulative_net_pnl=0,
                stock_market_value=0, corporate_action_receivable=0,
                corporate_action_income=0, pending_security_value=0,
                rights_subscription_receivable=0, updated_at=now() where {condition}
            """)).rowcount
            db.commit()
        except Exception:
            db.rollback()
            raise
    identifiers = set(ACCOUNT_IDS) | set(order_ids) | set(trade_ids) | set(position_ids) | set(detail_ids)
    keys = [key for key in redis_client.scan_iter(count=1000) if any(item in (key.decode() if isinstance(key, bytes) else str(key)) for item in identifiers)]
    for index in range(0, len(keys), 500):
        redis_client.delete(*keys[index:index + 500])
    redis_client.close()
    print(f"deleted_rows={sum(deleted)} accounts_reset={reset} redis_keys_deleted={len(keys)}")


if __name__ == "__main__":
    main()
