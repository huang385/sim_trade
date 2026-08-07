from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from app.core.database import SessionLocal
from app.core.redis_client import redis_client
from app.infrastructure.market_data.market_tick_store import MarketTickStore
from app.infrastructure.redis_keys import market_latest_key
from app.matching.models import MatchResult
from app.models.account import Account
from app.models.daily_settlement import DailySettlementBatch
from app.models.fee_rule_item import FeeRuleItem
from app.models.instrument import Instrument
from app.models.option_margin_rule import OptionMarginRule
from app.models.order import Order
from app.models.outbox_event import OutboxEvent
from app.models.position import Position
from app.models.trade import Trade
from app.repositories.outbox_repository import OutboxRepository
from app.schemas.market_tick_schema import MarketTick
from app.services.option_market_price_service import OptionMarginMarketPrices
from app.services.option_margin_adjustment_service import (
    OptionMarginAdjustmentService,
)
from app.services.option_order_margin_adjustment_service import (
    OptionOrderMarginAdjustmentService,
)
from app.services.option_trading_permission_service import (
    OptionTradingPermissionService,
)
from app.services.pnl_snapshot_persistence_service import (
    PnlSnapshotPersistenceService,
)
from app.services.realtime_fact_event_service import RealtimeFactEventService
from app.services.risk_monitor_service import RiskMonitorService
from app.services.trade_settlement_service import (
    SettlementCommand,
    TradeSettlementService,
)
from tests.integration.conftest import (
    make_cancellation_service,
    make_order_service,
    make_request,
)


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


class _IndexOptionPermissionConfig:
    """为集成测试显式开启股指期权买方和卖方权限。"""

    option_trading_enabled = True
    commodity_option_trading_enabled = True
    index_option_buy_trading_enabled = True
    index_option_short_trading_enabled = True


class _FixedIndexOptionPrices:
    @staticmethod
    def get_margin_prices(**_kwargs):
        return OptionMarginMarketPrices(
            option_price=Decimal("100"),
            underlying_price=Decimal("4000"),
        )


class _FailingOutboxRepository(OutboxRepository):
    """故障注入：模拟事务内事实Outbox无法创建。"""

    @staticmethod
    def create_event(*_args, **_kwargs):
        raise RuntimeError("injected outbox failure")


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
    with SessionLocal() as db:
        latest_settled_day = db.scalar(
            select(func.max(DailySettlementBatch.trading_day)).where(
                DailySettlementBatch.status == "COMPLETED"
            )
        )
    tick_trading_day = now.date()
    if latest_settled_day is not None and tick_trading_day <= latest_settled_day:
        tick_trading_day = latest_settled_day + timedelta(days=1)
    tick = MarketTick(
        source_event_id=f"IT-TICK-{uuid4().hex}",
        ingest_type="LIVE_CALLBACK",
        order_book_id=symbol,
        exchange_id=exchange_id,
        symbol=symbol,
        trading_day=tick_trading_day,
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

        # 期权空头持仓保证金上调必须把账户和持仓绝对事实与资金修改放在
        # 同一个PostgreSQL事务中，供已连接WebSocket增量更新。
        _put_live_tick(
            integration_context.exchange_id,
            integration_context.symbol,
            "3600",
        )
        _put_live_tick(
            integration_context.exchange_id,
            option_symbol,
            "120",
        )
        with SessionLocal() as db:
            short_position = db.scalar(
                select(Position).where(
                    Position.account_id == integration_context.account_id,
                    Position.order_book_id == option_symbol,
                    Position.direction == "SHORT",
                )
            )
            position_id = short_position.position_id
        with SessionLocal() as db:
            OptionMarginAdjustmentService(
                market_tick_store=MarketTickStore(redis_client)
            ).adjust(
                db,
                account_id=integration_context.account_id,
                position_id=position_id,
            )
        with SessionLocal() as db:
            margin_events = db.scalars(
                select(OutboxEvent)
                .where(
                    OutboxEvent.aggregate_id.in_(
                        (integration_context.account_id, position_id)
                    ),
                    OutboxEvent.event_type.in_(
                        ("ACCOUNT_FACT_UPDATED", "POSITION_UPDATED")
                    ),
                )
                .order_by(OutboxEvent.id.desc())
                .limit(2)
            ).all()
        assert {event.event_type for event in margin_events} == {
            "ACCOUNT_FACT_UPDATED",
            "POSITION_UPDATED",
        }
        by_type = {event.event_type: event.payload for event in margin_events}
        assert Decimal(
            by_type["ACCOUNT_FACT_UPDATED"]["used_margin"]
        ) > Decimal("0")
        assert Decimal(
            by_type["POSITION_UPDATED"]["used_margin"]
        ) > Decimal("0")

        # 真实Session故障注入：Outbox创建失败后，账户和持仓修改必须由
        # Service统一rollback，不能留下只有数据库事实没有事件的状态。
        with SessionLocal() as db:
            before_account = db.scalar(
                select(Account).where(
                    Account.account_id == integration_context.account_id
                )
            )
            before_position = db.scalar(
                select(Position).where(Position.position_id == position_id)
            )
            before_values = (
                before_account.used_margin,
                before_account.option_used_margin,
                before_position.used_margin,
                before_position.realtime_required_margin,
            )
        _put_live_tick(
            integration_context.exchange_id,
            integration_context.symbol,
            "3700",
        )
        _put_live_tick(
            integration_context.exchange_id,
            option_symbol,
            "130",
        )
        failing_facts = RealtimeFactEventService(
            repository=_FailingOutboxRepository()
        )
        with SessionLocal() as db:
            with pytest.raises(RuntimeError, match="injected outbox failure"):
                OptionMarginAdjustmentService(
                    market_tick_store=MarketTickStore(redis_client),
                    realtime_fact_events=failing_facts,
                ).adjust(
                    db,
                    account_id=integration_context.account_id,
                    position_id=position_id,
                )
        with SessionLocal() as db:
            rolled_back_account = db.scalar(
                select(Account).where(
                    Account.account_id == integration_context.account_id
                )
            )
            rolled_back_position = db.scalar(
                select(Position).where(Position.position_id == position_id)
            )
            assert (
                rolled_back_account.used_margin,
                rolled_back_account.option_used_margin,
                rolled_back_position.used_margin,
                rolled_back_position.realtime_required_margin,
            ) == before_values

        buy_close = create("BUY", "CLOSE_TODAY", "90")
        assert _settle(
            buy_close.order_id, "OPT-BUY-CLOSE", "90", 2
        ).action == "SETTLED"

        # 真实制造活动卖出开仓订单保证金缺口，验证PG风险来源、最终成交
        # 拦截以及行情恢复后的完整账户估值恢复，不注入任何保证金替身。
        deficit_order = create("SELL", "OPEN", "100")
        adjustment_service = OptionOrderMarginAdjustmentService(
            market_tick_store=MarketTickStore(redis_client)
        )
        # 当前行情高于接单冻结快照但资金仍充足，先验证补冻后的账户和
        # 订单绝对事实Outbox与数据库修改原子提交。
        with SessionLocal() as db:
            added = adjustment_service.adjust(
                db,
                order_id=deficit_order.order_id,
            )
        assert added.action == "ADDED"
        with SessionLocal() as db:
            added_events = db.scalars(
                select(OutboxEvent)
                .where(
                    OutboxEvent.aggregate_id.in_(
                        (
                            deficit_order.order_id,
                            integration_context.account_id,
                        )
                    ),
                    OutboxEvent.event_type.in_(
                        (
                            "ORDER_MARGIN_UPDATED",
                            "ACCOUNT_FACT_UPDATED",
                        )
                    ),
                )
                .order_by(OutboxEvent.id.desc())
                .limit(2)
            ).all()
        assert {event.event_type for event in added_events} == {
            "ORDER_MARGIN_UPDATED",
            "ACCOUNT_FACT_UPDATED",
        }
        order_margin_payload = next(
            event.payload
            for event in added_events
            if event.event_type == "ORDER_MARGIN_UPDATED"
        )
        assert Decimal(order_margin_payload["frozen_margin"]) == (
            added.frozen_margin
        )

        # 活动订单补冻也验证真实事务回滚；失败后订单和账户冻结额必须
        # 保持上一次成功提交值，并且不会产生半条成功事实。
        with SessionLocal() as db:
            before_order = db.scalar(
                select(Order).where(Order.order_id == deficit_order.order_id)
            )
            before_account = db.scalar(
                select(Account).where(
                    Account.account_id == integration_context.account_id
                )
            )
            before_frozen = (
                before_order.frozen_margin,
                before_account.frozen_margin,
            )
        _put_live_tick(
            integration_context.exchange_id,
            integration_context.symbol,
            "3800",
        )
        _put_live_tick(
            integration_context.exchange_id,
            option_symbol,
            "140",
        )
        with SessionLocal() as db:
            with pytest.raises(RuntimeError, match="injected outbox failure"):
                OptionOrderMarginAdjustmentService(
                    market_tick_store=MarketTickStore(redis_client),
                    realtime_fact_events=failing_facts,
                ).adjust(db, order_id=deficit_order.order_id)
        with SessionLocal() as db:
            rolled_back_order = db.scalar(
                select(Order).where(Order.order_id == deficit_order.order_id)
            )
            rolled_back_account = db.scalar(
                select(Account).where(
                    Account.account_id == integration_context.account_id
                )
            )
            assert (
                rolled_back_order.frozen_margin,
                rolled_back_account.frozen_margin,
            ) == before_frozen

        _put_live_tick(
            integration_context.exchange_id,
            integration_context.symbol,
            "1000000",
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

        # PnL持久化只更新完整估值事实，不越权推进风险状态机。真实运行中由
        # 500ms风险Worker依次确认RECOVERED和NORMAL，这里显式执行两轮复核。
        risk_monitor = RiskMonitorService(
            session_factory=SessionLocal,
            cancellation_service=make_cancellation_service(),
        )
        risk_monitor.process_account(integration_context.account_id)
        risk_monitor.process_account(integration_context.account_id)

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


def test_index_option_long_open_close_uses_real_postgres(integration_context):
    """真实验证股指期权买方开平仓、权利金、手续费、持仓和卖方禁用边界。"""

    suffix = uuid4().hex[:10].upper()
    index_symbol = f"IDX{suffix}"
    option_symbol = f"IO{suffix}-C-4000"
    option_id = None
    index_id = None
    with SessionLocal() as db:
        account = db.scalar(
            select(Account).where(
                Account.account_id == integration_context.account_id
            )
        )
        account.option_trading_enabled = True
        account.risk_available_cash = account.available_cash
        index = Instrument(
            order_book_id=index_symbol,
            symbol=index_symbol,
            exchange_id="CFFEX",
            instrument_name="集成测试标的指数",
            product_id="CSI300",
            market_type="INDEX",
            instrument_type="INDEX",
            contract_multiplier=Decimal("1"),
            price_tick=Decimal("0.01"),
            min_volume=1,
            max_volume=1,
            is_active=True,
            is_tradeable=False,
            data_source="INTERNAL",
        )
        db.add(index)
        db.flush()
        index_id = index.id
        option = Instrument(
            order_book_id=option_symbol,
            symbol=option_symbol,
            exchange_id="CFFEX",
            instrument_name="集成测试沪深300股指期权",
            product_id="IO",
            market_type="FUTURES",
            instrument_type="INDEX_OPTION",
            underlying_instrument_id=index.id,
            option_type="CALL",
            strike_price=Decimal("4000"),
            exercise_style="EUROPEAN",
            settlement_type="CASH",
            contract_multiplier=Decimal("100"),
            price_tick=Decimal("0.2"),
            min_volume=1,
            max_volume=100,
            expire_date=integration_context.trading_day + timedelta(days=90),
            is_active=True,
            is_tradeable=True,
            data_source="INTERNAL",
        )
        db.add(option)
        db.flush()
        option_id = option.id
        db.add(
            OptionMarginRule(
                exchange_id="CFFEX",
                product_id="IO",
                instrument_id=option.id,
                instrument_type="INDEX_OPTION",
                margin_algorithm="CFFEX_INDEX_OPTION",
                margin_adjustment_rate=Decimal("0.12"),
                minimum_guarantee_rate=Decimal("0.07"),
                out_of_money_deduction_rate=Decimal("1"),
                minimum_underlying_margin_ratio=Decimal("0"),
                extra_margin_rate=Decimal("0"),
                trading_day=integration_context.trading_day,
                rule_version=f"IT-{suffix}-MARGIN",
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
                    exchange_id="CFFEX",
                    product_id="IO",
                    instrument_id=option.id,
                    instrument_type="INDEX_OPTION",
                    direction=direction,
                    offset_flag=offset_flag,
                    commission_type="BY_VOLUME",
                    commission_parameter=Decimal("15"),
                    trading_day=integration_context.trading_day,
                    rule_version=f"IT-{suffix}-{direction}-{offset_flag}",
                    data_source="INTERNAL",
                    is_active=True,
                )
            )
        db.commit()

    try:
        order_service = make_order_service(integration_context)
        order_service.option_permission_service = (
            OptionTradingPermissionService(_IndexOptionPermissionConfig())
        )
        order_service.option_market_price_service = _FixedIndexOptionPrices()
        _put_live_tick("CFFEX", index_symbol, "4000")
        _put_live_tick("CFFEX", option_symbol, "100")

        with SessionLocal() as db:
            buy_open = order_service.create_order(
                db,
                make_request(
                    integration_context,
                    client_order_id=f"IDXOPT-OPEN-{suffix}",
                    exchange_id="CFFEX",
                    symbol=option_symbol,
                    direction="BUY",
                    offset_flag="OPEN",
                    limit_price=Decimal("100"),
                    volume=2,
                ),
            )
        assert buy_open.status == "ACCEPTED"
        assert buy_open.instrument_type == "INDEX_OPTION"
        assert buy_open.frozen_cash == Decimal("20000.000000")
        assert buy_open.frozen_margin == Decimal("0.000000")
        assert buy_open.frozen_commission == Decimal("30.000000")
        assert _settle(
            buy_open.order_id,
            f"IDXOPT-OPEN-{suffix}",
            "98",
            2,
        ).action == "SETTLED"

        with SessionLocal() as db:
            trade = db.scalar(
                select(Trade).where(Trade.order_id == buy_open.order_id)
            )
            position = db.scalar(
                select(Position).where(
                    Position.account_id == integration_context.account_id,
                    Position.order_book_id == option_symbol,
                    Position.direction == "LONG",
                )
            )
            account = db.scalar(
                select(Account).where(
                    Account.account_id == integration_context.account_id
                )
            )
            assert trade.turnover == Decimal("19600.000000")
            assert trade.premium_cash_flow == Decimal("-19600.000000")
            assert trade.commission == Decimal("30.000000")
            assert trade.margin == Decimal("0.000000")
            assert position.total_volume == 2
            assert position.average_open_price == Decimal("98.000000")
            assert position.option_market_value == Decimal("19600.000000")
            assert account.used_margin == Decimal("0.000000")
            assert account.daily_commission == Decimal("30.000000")

        with SessionLocal() as db:
            sell_close = order_service.create_order(
                db,
                make_request(
                    integration_context,
                    client_order_id=f"IDXOPT-CLOSE-{suffix}",
                    exchange_id="CFFEX",
                    symbol=option_symbol,
                    direction="SELL",
                    offset_flag="CLOSE_TODAY",
                    limit_price=Decimal("105"),
                    volume=2,
                ),
            )
        assert _settle(
            sell_close.order_id,
            f"IDXOPT-CLOSE-{suffix}",
            "105",
            2,
        ).action == "SETTLED"

        with SessionLocal() as db:
            position = db.scalar(
                select(Position).where(
                    Position.account_id == integration_context.account_id,
                    Position.order_book_id == option_symbol,
                    Position.direction == "LONG",
                )
            )
            account = db.scalar(
                select(Account).where(
                    Account.account_id == integration_context.account_id
                )
            )
            assert position.total_volume == 0
            assert position.realized_pnl == Decimal("1400.000000")
            assert account.daily_commission == Decimal("60.000000")
            assert account.daily_close_pnl == Decimal("1400.000000")
            assert account.daily_pnl == Decimal("1340.000000")

        with SessionLocal() as db:
            sell_open = order_service.create_order(
                db,
                make_request(
                    integration_context,
                    client_order_id=f"IDXOPT-SHORT-{suffix}",
                    exchange_id="CFFEX",
                    symbol=option_symbol,
                    direction="SELL",
                    offset_flag="OPEN",
                    limit_price=Decimal("100"),
                    volume=1,
                ),
            )
        # 每手：权利金100*100 + 指数4000*100*12% = 58000。
        assert sell_open.frozen_margin == Decimal("58000.000000")
        assert sell_open.frozen_cash == Decimal("0.000000")
        assert sell_open.frozen_commission == Decimal("15.000000")
        assert sell_open.margin_rule_snapshot["margin_algorithm"] == (
            "CFFEX_INDEX_OPTION"
        )
        assert "underlying_margin_rate" not in sell_open.margin_rule_snapshot
        assert _settle(
            sell_open.order_id,
            f"IDXOPT-SHORT-{suffix}",
            "98",
            1,
        ).action == "SETTLED"

        with SessionLocal() as db:
            short_position = db.scalar(
                select(Position).where(
                    Position.account_id == integration_context.account_id,
                    Position.order_book_id == option_symbol,
                    Position.direction == "SHORT",
                )
            )
            short_trade = db.scalar(
                select(Trade).where(Trade.order_id == sell_open.order_id)
            )
            assert short_trade.premium_cash_flow == Decimal("9800.000000")
            assert short_trade.margin == Decimal("58000.000000")
            assert short_position.total_volume == 1
            assert short_position.used_margin == Decimal("58000.000000")

        # 定时持久化也必须通过同一解析器重算股指期权保证金，不能退回
        # 商品期权公式或直接信任Redis中的金额。
        with SessionLocal() as db:
            account = db.scalar(
                select(Account)
                .where(
                    Account.account_id == integration_context.account_id
                )
                .with_for_update()
            )
            recalculated = PnlSnapshotPersistenceService(
                session_factory=SessionLocal,
                pnl_store=None,
                market_tick_store=MarketTickStore(redis_client),
            )._recalculate_locked_account(db, account)
            db.commit()
        assert recalculated is True

        with SessionLocal() as db:
            buy_close = order_service.create_order(
                db,
                make_request(
                    integration_context,
                    client_order_id=f"IDXOPT-SHORT-CLOSE-{suffix}",
                    exchange_id="CFFEX",
                    symbol=option_symbol,
                    direction="BUY",
                    offset_flag="CLOSE_TODAY",
                    limit_price=Decimal("90"),
                    volume=1,
                ),
            )
        assert _settle(
            buy_close.order_id,
            f"IDXOPT-SHORT-CLOSE-{suffix}",
            "90",
            1,
        ).action == "SETTLED"

        # 平仓提交与500ms估值是异步链路；再做一次账户级完整核对，清除
        # 已关闭空头在上一周期留下的浮动盈亏。
        with SessionLocal() as db:
            account = db.scalar(
                select(Account)
                .where(
                    Account.account_id == integration_context.account_id
                )
                .with_for_update()
            )
            assert PnlSnapshotPersistenceService(
                session_factory=SessionLocal,
                pnl_store=None,
                market_tick_store=MarketTickStore(redis_client),
            )._recalculate_locked_account(db, account)
            db.commit()

        with SessionLocal() as db:
            short_position = db.scalar(
                select(Position).where(
                    Position.account_id == integration_context.account_id,
                    Position.order_book_id == option_symbol,
                    Position.direction == "SHORT",
                )
            )
            account = db.scalar(
                select(Account).where(
                    Account.account_id == integration_context.account_id
                )
            )
            assert short_position.total_volume == 0
            assert short_position.realized_pnl == Decimal("800.000000")
            assert account.used_margin == Decimal("0.000000")
            assert account.option_used_margin == Decimal("0.000000")
            assert account.daily_commission == Decimal("90.000000")
            assert account.daily_close_pnl == Decimal("2200.000000")
            assert account.daily_pnl == Decimal("2110.000000")
    finally:
        with SessionLocal() as db:
            if option_id is not None:
                db.execute(
                    delete(OptionMarginRule).where(
                        OptionMarginRule.instrument_id == option_id
                    )
                )
                db.execute(
                    delete(FeeRuleItem).where(
                        FeeRuleItem.instrument_id == option_id
                    )
                )
                db.execute(
                    delete(Instrument).where(Instrument.id == option_id)
                )
            if index_id is not None:
                db.execute(
                    delete(Instrument).where(Instrument.id == index_id)
                )
            db.commit()
