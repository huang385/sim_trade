from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.core.database import SessionLocal
from app.core.redis_client import redis_client
from app.infrastructure.market_data.market_tick_store import MarketTickStore
from app.infrastructure.redis_keys import market_latest_key
from app.matching.models import MatchResult
from app.models.account import Account
from app.models.fee_rule_item import FeeRuleItem
from app.models.instrument import Instrument
from app.models.option_margin_rule import OptionMarginRule
from app.models.position import Position
from app.models.trade import Trade
from app.schemas.market_tick_schema import MarketTick
from app.services.option_market_price_service import OptionMarginMarketPrices
from app.services.option_order_margin_adjustment_service import (
    OptionOrderMarginAdjustmentService,
)
from app.services.pnl_snapshot_persistence_service import (
    PnlSnapshotPersistenceService,
)
from app.services.trade_settlement_service import (
    SettlementCommand,
    TradeSettlementService,
)
from tests.integration.conftest import make_order_service, make_request


pytestmark = pytest.mark.integration


class _AllowOptionTrading:
    @staticmethod
    def validate(**_kwargs):
        return None


class _FixedOptionPrices:
    @staticmethod
    def get_margin_prices(**_kwargs):
        return OptionMarginMarketPrices(
            option_price=Decimal("100"),
            underlying_price=Decimal("3500"),
        )


def _settle(order_id: str, event_id: str, price: str, volume: int):
    with SessionLocal() as db:
        # 不注入保证金校验替身：商品期权卖出开仓必须实际读取Redis中的
        # 期权和标的行情，并执行成交前最终保证金校验。
        return TradeSettlementService().settle(
            db,
            SettlementCommand(
                order_id=order_id,
                market_event_id=event_id,
                market_stream_message_id=f"{event_id}-0",
                tick_event_time=datetime.now(timezone.utc),
                tick_sequence_id=1,
                match_result=MatchResult(
                    matched=True,
                    fill_price=Decimal(price),
                    fill_volume=volume,
                    reason=None,
                    engine_name="VN",
                    engine_version="1.0",
                ),
            ),
        )


def _put_live_tick(exchange_id: str, symbol: str, price: str) -> None:
    now = datetime.now(timezone.utc)
    tick = MarketTick(
        source_event_id=f"IT-TICK-{uuid4().hex}",
        ingest_type="LIVE_CALLBACK",
        order_book_id=symbol,
        exchange_id=exchange_id,
        symbol=symbol,
        trading_day=now.date(),
        event_time=now,
        local_recv_time=now,
        sequence_id=1,
        last_price=Decimal(price),
        cumulative_volume=1,
        bid_volume_1=0,
        ask_volume_1=0,
    )
    redis_client.hset(
        market_latest_key(exchange_id, symbol),
        mapping=MarketTickStore.tick_to_mapping(tick),
    )


def test_commodity_option_four_directions_use_real_postgres(
    integration_context,
):
    """真实数据库验证BUY/SELL开仓及对应SELL/BUY平仓完整资源守恒。"""

    option_symbol = f"{integration_context.symbol}-C-3500"
    with SessionLocal() as db:
        account = db.scalar(
            select(Account).where(
                Account.account_id == integration_context.account_id
            )
        )
        underlying = db.scalar(
            select(Instrument).where(
                Instrument.exchange_id == integration_context.exchange_id,
                Instrument.symbol == integration_context.symbol,
            )
        )
        account.risk_available_cash = account.available_cash
        option = Instrument(
            order_book_id=option_symbol,
            symbol=option_symbol,
            exchange_id=integration_context.exchange_id,
            instrument_name="集成测试商品期权",
            product_id="IT",
            market_type="FUTURES",
            instrument_type="FUTURES_OPTION",
            underlying_instrument_id=underlying.id,
            option_type="CALL",
            strike_price=Decimal("3500"),
            contract_multiplier=Decimal("10"),
            price_tick=Decimal("1"),
            min_volume=1,
            max_volume=100,
            expire_date=(
                integration_context.trading_day + timedelta(days=365)
            ),
            is_active=True,
            is_tradeable=True,
            data_source="INTERNAL",
        )
        db.add(option)
        db.flush()
        db.add(
            OptionMarginRule(
                exchange_id=integration_context.exchange_id,
                product_id="IT",
                instrument_id=option.id,
                instrument_type="FUTURES_OPTION",
                margin_algorithm="COMMODITY_FUTURES_OPTION",
                margin_adjustment_rate=Decimal("1"),
                minimum_guarantee_rate=Decimal("0"),
                out_of_money_deduction_rate=Decimal("1"),
                minimum_underlying_margin_ratio=Decimal("0.5"),
                extra_margin_rate=Decimal("0"),
                trading_day=integration_context.trading_day,
                rule_version="IT-V1",
                data_source="INTERNAL",
                is_active=True,
            )
        )
        for direction, offset_flag in (
            ("BUY", "OPEN"),
            ("SELL", "OPEN"),
            ("SELL", "CLOSE_TODAY"),
            ("BUY", "CLOSE_TODAY"),
        ):
            db.add(
                FeeRuleItem(
                    exchange_id=integration_context.exchange_id,
                    product_id="IT",
                    instrument_id=option.id,
                    instrument_type="FUTURES_OPTION",
                    direction=direction,
                    offset_flag=offset_flag,
                    commission_type="BY_VOLUME",
                    commission_parameter=Decimal("1"),
                    trading_day=integration_context.trading_day,
                    rule_version=f"IT-{direction}-{offset_flag}",
                    data_source="INTERNAL",
                    is_active=True,
                )
            )
        db.commit()

    try:
        order_service = make_order_service(integration_context)
        order_service.option_permission_service = _AllowOptionTrading()
        order_service.option_market_price_service = _FixedOptionPrices()
        _put_live_tick(
            integration_context.exchange_id,
            integration_context.symbol,
            "3500",
        )
        _put_live_tick(
            integration_context.exchange_id,
            option_symbol,
            "100",
        )

        def create(direction: str, offset_flag: str, price: str):
            with SessionLocal() as db:
                return order_service.create_order(
                    db,
                    make_request(
                        integration_context,
                        client_order_id=f"OPT-{uuid4().hex}",
                        symbol=option_symbol,
                        direction=direction,
                        offset_flag=offset_flag,
                        limit_price=Decimal(price),
                        volume=2,
                    ),
                )

        buy_open = create("BUY", "OPEN", "100")
        assert _settle(
            buy_open.order_id, "OPT-BUY-OPEN", "100", 2
        ).action == "SETTLED"
        sell_close = create("SELL", "CLOSE_TODAY", "110")
        assert _settle(
            sell_close.order_id, "OPT-SELL-CLOSE", "110", 2
        ).action == "SETTLED"

        sell_open = create("SELL", "OPEN", "100")
        assert sell_open.frozen_margin > Decimal("0")
        assert _settle(
            sell_open.order_id, "OPT-SELL-OPEN", "100", 2
        ).action == "SETTLED"
        buy_close = create("BUY", "CLOSE_TODAY", "90")
        assert _settle(
            buy_close.order_id, "OPT-BUY-CLOSE", "90", 2
        ).action == "SETTLED"

        # 真实制造活动卖出开仓订单保证金缺口，验证PG风险来源、最终成交
        # 拦截以及行情恢复后的完整账户估值恢复，不注入任何保证金替身。
        deficit_order = create("SELL", "OPEN", "100")
        _put_live_tick(
            integration_context.exchange_id,
            integration_context.symbol,
            "1000000",
        )
        adjustment_service = OptionOrderMarginAdjustmentService(
            market_tick_store=MarketTickStore(redis_client)
        )
        with SessionLocal() as db:
            deficit = adjustment_service.adjust(
                db,
                order_id=deficit_order.order_id,
            )
        assert deficit.action == "MARGIN_DEFICIT"
        assert _settle(
            deficit_order.order_id,
            "OPT-DEFICIT-BLOCKED",
            "100",
            2,
        ).action == "RISK_BLOCKED"

        _put_live_tick(
            integration_context.exchange_id,
            integration_context.symbol,
            "3500",
        )
        with SessionLocal() as db:
            recovered_order = adjustment_service.adjust(
                db,
                order_id=deficit_order.order_id,
            )
        assert recovered_order.action == "RECOVERED"
        with SessionLocal() as db:
            account = db.scalar(
                select(Account)
                .where(
                    Account.account_id == integration_context.account_id
                )
                .with_for_update()
            )
            complete = PnlSnapshotPersistenceService(
                session_factory=SessionLocal,
                pnl_store=None,
                market_tick_store=MarketTickStore(redis_client),
            )._recalculate_locked_account(db, account)
            db.commit()
        assert complete is True

        with SessionLocal() as db:
            trades = db.scalars(
                select(Trade)
                .where(Trade.account_id == integration_context.account_id)
                .order_by(Trade.id)
            ).all()
            positions = db.scalars(
                select(Position).where(
                    Position.account_id == integration_context.account_id,
                    Position.order_book_id == option_symbol,
                )
            ).all()
            account = db.scalar(
                select(Account).where(
                    Account.account_id == integration_context.account_id
                )
            )

            assert [
                (trade.direction, trade.offset_flag)
                for trade in trades
            ] == [
                ("BUY", "OPEN"),
                ("SELL", "CLOSE_TODAY"),
                ("SELL", "OPEN"),
                ("BUY", "CLOSE_TODAY"),
            ]
            assert all(position.total_volume == 0 for position in positions)
            assert account.used_margin == Decimal("0.000000")
            assert account.option_used_margin == Decimal("0.000000")
            assert account.option_realtime_required_margin == Decimal(
                "0.000000"
            )
            assert account.long_option_market_value == Decimal("0.000000")
            assert account.short_option_market_value == Decimal("0.000000")
            assert account.risk_state == "NORMAL"
    finally:
        # 订单、成交、持仓由通用integration_context夹具清理；这里先删除
        # 只属于本测试的规则与期权合约，避免污染用户现有参考数据。
        with SessionLocal() as db:
            option_id = db.scalar(
                select(Instrument.id).where(
                    Instrument.exchange_id
                    == integration_context.exchange_id,
                    Instrument.symbol == option_symbol,
                )
            )
            if option_id is not None:
                db.execute(
                    delete(FeeRuleItem).where(
                        FeeRuleItem.instrument_id == option_id
                    )
                )
                db.execute(
                    delete(OptionMarginRule).where(
                        OptionMarginRule.instrument_id == option_id
                    )
                )
                db.execute(
                    delete(Instrument).where(Instrument.id == option_id)
                )
            db.commit()
        redis_client.delete(
            market_latest_key(
                integration_context.exchange_id,
                option_symbol,
            ),
            market_latest_key(
                integration_context.exchange_id,
                integration_context.symbol,
            ),
        )
