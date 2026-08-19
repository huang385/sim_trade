import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from redis.exceptions import RedisError
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.database import Base, engine as primary_engine
from app.core.redis_client import redis_client
from app.infrastructure.market_data.market_tick_store import MarketTickStore
from app.infrastructure.redis_keys import market_latest_key
from app.models.account import Account
from app.models.app_user import AppUser
from app.models.daily_settlement import (
    DailyAccountSettlement,
    DailyPositionSettlement,
    DailySettlementBatch,
    InstrumentSettlementPrice,
    OptionExpirySettlementDetail,
)
from app.models.instrument import Instrument
from app.models.margin_rule_daily import MarginRuleDaily
from app.models.order import Order
from app.models.outbox_event import OutboxEvent
from app.models.position import Position
from app.models.position_detail import PositionDetail
from app.models.trade import Trade
from app.schemas.market_tick_schema import MarketTick, MarketTickIngestType
from app.services.daily_settlement_service import (
    DailySettlementError,
    DailySettlementService,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def isolated_settlement_database():
    schema = f"it_daily_{uuid4().hex[:12]}"
    assert re.fullmatch(r"it_daily_[0-9a-f]{12}", schema)
    try:
        with primary_engine.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    except SQLAlchemyError as exc:
        pytest.skip(f"真实 PostgreSQL 不可用或无建 schema 权限: {exc}")

    isolated_engine = create_engine(
        settings.database_url,
        poolclass=NullPool,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    try:
        Base.metadata.create_all(isolated_engine)
        with isolated_engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE trading_calendar ("
                    "id bigserial PRIMARY KEY, exchange_id varchar(32) NOT NULL, "
                    "trading_day date NOT NULL, previous_trading_day date, "
                    "next_trading_day date, is_open boolean NOT NULL, "
                    "status varchar(16) NOT NULL)"
                )
            )
            connection.execute(
                text(
                    "CREATE TABLE product_trading_schedule ("
                    "id bigserial PRIMARY KEY, trading_day date NOT NULL, "
                    "exchange_id varchar(32) NOT NULL, product_code varchar(64) NOT NULL, "
                    "instrument_type varchar(32) NOT NULL, sessions jsonb NOT NULL, "
                    "status varchar(16) NOT NULL, "
                    "representative_order_book_id varchar(64) NOT NULL)"
                )
            )
        yield isolated_engine, sessionmaker(
            bind=isolated_engine,
            expire_on_commit=False,
        )
    finally:
        isolated_engine.dispose()
        with primary_engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')


def _account(account_id: str, user_id: str, trading_day) -> Account:
    return Account(
        account_id=account_id,
        user_id=user_id,
        account_name="真实日结测试账户",
        account_type="FUTURES",
        option_trading_enabled=True,
        initial_cash=Decimal("10000"),
        cash_balance=Decimal("10000"),
        available_cash=Decimal("9699"),
        frozen_cash=Decimal("0"),
        equity=Decimal("10000"),
        used_margin=Decimal("200"),
        frozen_margin=Decimal("100"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        daily_position_pnl=Decimal("0"),
        option_used_margin=Decimal("0"),
        option_realtime_required_margin=Decimal("0"),
        long_option_market_value=Decimal("0"),
        short_option_market_value=Decimal("0"),
        net_option_market_value=Decimal("0"),
        risk_available_cash=Decimal("9699"),
        risk_state="NORMAL",
        daily_close_pnl=Decimal("25"),
        daily_commission=Decimal("5"),
        daily_pnl=Decimal("20"),
        used_commission=Decimal("5"),
        frozen_commission=Decimal("1"),
        risk_ratio=Decimal("0"),
        status="NORMAL",
        trading_day=trading_day,
    )


def _position(
    *, position_id, account_id, code, instrument_type, volume, price
) -> Position:
    return Position(
        position_id=position_id,
        account_id=account_id,
        order_book_id=code,
        exchange_id="ITDS",
        symbol=code,
        instrument_type=instrument_type,
        direction="LONG",
        total_volume=volume,
        today_volume=volume,
        yesterday_volume=0,
        frozen_volume=0,
        available_volume=volume,
        average_open_price=price,
        position_cost=price * Decimal(volume) * Decimal("10"),
        used_margin=Decimal("200") if instrument_type == "FUTURES" else Decimal("0"),
        initial_occupied_margin=(
            Decimal("200") if instrument_type == "FUTURES" else Decimal("0")
        ),
        realtime_required_margin=Decimal("0"),
        option_market_value=Decimal("0"),
        multiplier_snapshot=Decimal("10"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        daily_position_pnl=Decimal("0"),
        daily_close_pnl=Decimal("0"),
        trading_day=None,
    )


def _detail(
    *, detail_id, position_id, account_id, code, instrument_type, price, base, volume
) -> PositionDetail:
    margin = Decimal("100") if instrument_type == "FUTURES" else Decimal("0")
    return PositionDetail(
        position_detail_id=detail_id,
        position_id=position_id,
        account_id=account_id,
        open_trade_id=f"TRADE-{detail_id}",
        order_book_id=code,
        exchange_id="ITDS",
        symbol=code,
        instrument_type=instrument_type,
        direction="LONG",
        open_trading_day=None,
        open_price=price,
        pnl_base_price=base,
        original_volume=volume,
        remaining_volume=volume,
        frozen_volume=0,
        open_margin=margin,
        remaining_margin=margin,
        initial_occupied_margin=margin,
        realtime_required_margin=Decimal("0"),
        multiplier_snapshot=Decimal("10"),
        open_commission=Decimal("0"),
        status="OPEN",
    )


def _open_trade(detail: PositionDetail, *, now: datetime) -> Trade:
    return Trade(
        trade_id=detail.open_trade_id,
        order_id=f"ORDER-{detail.open_trade_id}",
        account_id=detail.account_id,
        market_event_id=f"EVENT-{detail.open_trade_id}",
        market_stream_message_id=f"STREAM-{detail.open_trade_id}",
        order_book_id=detail.order_book_id,
        exchange_id=detail.exchange_id,
        symbol=detail.symbol,
        trading_day=detail.open_trading_day,
        instrument_type=detail.instrument_type,
        direction="BUY",
        offset_flag="OPEN",
        trade_price=detail.open_price,
        trade_volume=detail.original_volume,
        turnover=(
            Decimal(detail.open_price)
            * Decimal(detail.original_volume)
            * Decimal(detail.multiplier_snapshot)
        ),
        margin=Decimal(detail.open_margin),
        premium_cash_flow=Decimal("0"),
        commission=Decimal("0"),
        realized_pnl=Decimal("0"),
        daily_close_pnl=Decimal("0"),
        trade_time=now,
        created_at=now,
    )


def _publish_tick(store, *, code, trading_day, now, price, sequence):
    store.publish(
        MarketTick(
            source_event_id=f"IT-DS-{sequence}-{uuid4().hex}",
            ingest_type=MarketTickIngestType.LIVE_CALLBACK,
            order_book_id=code,
            exchange_id="ITDS",
            symbol=code,
            trading_day=trading_day,
            event_time=now,
            sequence_id=sequence,
            last_price=price,
            cumulative_volume=sequence,
            bid_volume_1=1,
            ask_volume_1=1,
        )
    )


@pytest.mark.integration
def test_daily_settlement_keeps_post_corporate_action_cash_security_volume(
    isolated_settlement_database,
):
    """Cash securities are not reconstructed from derivative PositionDetail rows.

    The position below represents a stock holding after a 10-for-1 stock
    dividend has already been listed.  The full settlement path must only
    carry its quantity from today to yesterday; it must not restore the old
    100-share quantity from the derivative replay chain.
    """
    isolated_engine, factory = isolated_settlement_database
    now = datetime.now(timezone.utc)
    trading_day = now.astimezone(SHANGHAI).date()
    next_day = trading_day + timedelta(days=1)
    suffix = uuid4().hex[:8].upper()
    code = f"S{suffix}"
    account_id = f"SA{suffix}"
    user_id = f"SU{suffix}"
    stream_name = f"it:daily:settlement:cash-security:{suffix}"
    tick_store = MarketTickStore(redis_client, stream_name=stream_name)

    try:
        redis_client.ping()
    except RedisError as exc:
        pytest.skip(f"real Redis is unavailable: {exc}")

    with factory() as db:
        db.add(
            AppUser(
                user_id=user_id,
                username=f"cash_daily_{suffix.lower()}",
                password_hash="!",
                display_name="cash security settlement test",
            )
        )
        db.flush()
        account = _account(account_id, user_id, trading_day)
        account.account_type = "SECURITIES_CASH"
        account.available_cash = Decimal("10000")
        account.risk_available_cash = Decimal("10000")
        account.used_margin = account.frozen_margin = Decimal("0")
        cash_stock = Instrument(
            order_book_id=code,
            symbol=code,
            exchange_id="ITDS",
            instrument_name="post corporate action stock",
            product_id="ITP",
            market_type="STOCK",
            instrument_type="STOCK",
            contract_multiplier=Decimal("1"),
            price_tick=Decimal("0.01"),
            min_volume=1,
            max_volume=100000,
            is_active=True,
            data_source="INTERNAL",
        )
        # The historical cost remains 1,000 after 100 shares receive 10
        # listed shares.  No cash-security PositionDetail is intentionally
        # created: those details belong to the derivative replay domain.
        position = _position(
            position_id=f"PS{suffix}",
            account_id=account_id,
            code=code,
            instrument_type="STOCK",
            volume=110,
            price=Decimal("10"),
        )
        position.multiplier_snapshot = Decimal("1")
        position.position_cost = Decimal("1000")
        position.average_open_price = Decimal("9.090909")
        position.mark_price = Decimal("10")
        position.mark_time = now
        position.mark_source_event_id = f"POST-ACTION-MARK-{suffix}"
        position.market_value = Decimal("1100")
        position.unrealized_pnl = Decimal("100")
        position.daily_pnl_base_cost = Decimal("1000")
        position.yesterday_pnl_base_cost = Decimal("1000")
        position.today_pnl_base_cost = Decimal("0")
        position.daily_pnl_base_established = True
        db.add_all([account, cash_stock, position])
        session_end = (now - timedelta(minutes=1)).astimezone(SHANGHAI)
        db.execute(
            text(
                "INSERT INTO trading_calendar "
                "(exchange_id, trading_day, previous_trading_day, next_trading_day, "
                "is_open, status) VALUES "
                "(:exchange, :day, :previous, :next, true, 'OPEN')"
            ),
            {
                "exchange": "ITDS",
                "day": trading_day,
                "previous": trading_day - timedelta(days=1),
                "next": next_day,
            },
        )
        db.execute(
            text(
                "INSERT INTO product_trading_schedule "
                "(trading_day, exchange_id, product_code, instrument_type, "
                "sessions, status, representative_order_book_id) VALUES "
                "(:day, 'ITDS', 'ITP', 'STOCK', CAST(:sessions AS jsonb), "
                "'READY', :representative)"
            ),
            {
                "day": trading_day,
                "sessions": json.dumps(
                    [{
                        "start_at": (session_end - timedelta(hours=1)).isoformat(),
                        "end_at": session_end.isoformat(),
                    }]
                ),
                "representative": code,
            },
        )
        db.commit()

    _publish_tick(
        tick_store,
        code=code,
        trading_day=trading_day,
        now=now,
        price=Decimal("10"),
        sequence=1,
    )
    try:
        result = DailySettlementService(
            session_factory=factory,
            database_engine=isolated_engine,
            tick_store=tick_store,
            time_provider=lambda: now,
            redis_recovery_enabled=False,
        ).run(trading_day)
        assert result.already_completed is False
        with factory() as db:
            settled = db.scalar(
                select(Position).where(Position.position_id == position.position_id)
            )
            assert settled.total_volume == 110
            assert settled.today_volume == 0
            assert settled.yesterday_volume == 110
            assert settled.available_volume == 110
            assert settled.position_cost == Decimal("1000.000000")
    finally:
        pipeline = redis_client.pipeline(transaction=False)
        pipeline.delete(market_latest_key("ITDS", code))
        pipeline.delete(stream_name)
        pipeline.execute()


@pytest.mark.integration
def test_real_postgres_redis_daily_settlement_is_concurrent_and_idempotent(
    isolated_settlement_database,
):
    isolated_engine, factory = isolated_settlement_database
    now = datetime.now(timezone.utc)
    trading_day = now.astimezone(SHANGHAI).date()
    next_day = trading_day + timedelta(days=1)
    suffix = uuid4().hex[:8].upper()
    future_code = f"F{suffix}"
    expired_code = f"C{suffix}"
    live_code = f"L{suffix}"
    account_id = f"A{suffix}"
    user_id = f"U{suffix}"
    stream_name = f"it:daily:settlement:ticks:{suffix}"
    tick_store = MarketTickStore(redis_client, stream_name=stream_name)

    try:
        redis_client.ping()
    except RedisError as exc:
        pytest.skip(f"真实 Redis 不可用: {exc}")

    with factory() as db:
        db.add(
            AppUser(
                user_id=user_id,
                username=f"daily_{suffix.lower()}",
                password_hash="!",
                display_name="真实日结测试",
            )
        )
        db.flush()
        account = _account(account_id, user_id, trading_day)
        future = Instrument(
            order_book_id=future_code,
            symbol=future_code,
            exchange_id="ITDS",
            instrument_name="真实测试期货",
            product_id="ITP",
            market_type="FUTURES",
            instrument_type="FUTURES",
            contract_multiplier=Decimal("10"),
            price_tick=Decimal("1"),
            min_volume=1,
            max_volume=100,
            is_active=True,
            data_source="INTERNAL",
        )
        db.add_all([account, future])
        db.flush()
        expired = Instrument(
            order_book_id=expired_code,
            symbol=expired_code,
            exchange_id="ITDS",
            instrument_name="真实测试到期期权",
            product_id="ITP",
            market_type="OPTION",
            instrument_type="FUTURES_OPTION",
            contract_multiplier=Decimal("10"),
            price_tick=Decimal("0.1"),
            min_volume=1,
            max_volume=100,
            is_active=True,
            data_source="INTERNAL",
            underlying_instrument_id=future.id,
            option_type="CALL",
            strike_price=Decimal("110"),
            expire_date=trading_day,
        )
        live = Instrument(
            order_book_id=live_code,
            symbol=live_code,
            exchange_id="ITDS",
            instrument_name="真实测试未到期期权",
            product_id="ITP",
            market_type="OPTION",
            instrument_type="FUTURES_OPTION",
            contract_multiplier=Decimal("10"),
            price_tick=Decimal("0.1"),
            min_volume=1,
            max_volume=100,
            is_active=True,
            data_source="INTERNAL",
            underlying_instrument_id=future.id,
            option_type="CALL",
            strike_price=Decimal("130"),
            expire_date=trading_day + timedelta(days=30),
        )
        positions = [
            _position(
                position_id=f"PF{suffix}",
                account_id=account_id,
                code=future_code,
                instrument_type="FUTURES",
                volume=2,
                price=Decimal("95"),
            ),
            _position(
                position_id=f"PE{suffix}",
                account_id=account_id,
                code=expired_code,
                instrument_type="FUTURES_OPTION",
                volume=2,
                price=Decimal("5"),
            ),
            _position(
                position_id=f"PL{suffix}",
                account_id=account_id,
                code=live_code,
                instrument_type="FUTURES_OPTION",
                volume=1,
                price=Decimal("6"),
            ),
        ]
        for position in positions:
            position.trading_day = trading_day
        details = [
            _detail(
                detail_id=f"DF1{suffix}",
                position_id=f"PF{suffix}",
                account_id=account_id,
                code=future_code,
                instrument_type="FUTURES",
                price=Decimal("90"),
                base=Decimal("100"),
                volume=1,
            ),
            _detail(
                detail_id=f"DF2{suffix}",
                position_id=f"PF{suffix}",
                account_id=account_id,
                code=future_code,
                instrument_type="FUTURES",
                price=Decimal("100"),
                base=Decimal("105"),
                volume=1,
            ),
            _detail(
                detail_id=f"DE{suffix}",
                position_id=f"PE{suffix}",
                account_id=account_id,
                code=expired_code,
                instrument_type="FUTURES_OPTION",
                price=Decimal("5"),
                base=Decimal("5"),
                volume=2,
            ),
            _detail(
                detail_id=f"DL{suffix}",
                position_id=f"PL{suffix}",
                account_id=account_id,
                code=live_code,
                instrument_type="FUTURES_OPTION",
                price=Decimal("6"),
                base=Decimal("6"),
                volume=1,
            ),
        ]
        for detail in details:
            detail.open_trading_day = trading_day
        trades = [_open_trade(detail, now=now) for detail in details]
        order = Order(
            order_id=f"O{suffix}",
            client_order_id=f"CO{suffix}",
            account_id=account_id,
            order_book_id=future_code,
            symbol=future_code,
            exchange_id="ITDS",
            trading_day=trading_day,
            instrument_type="FUTURES",
            direction="BUY",
            offset_flag="OPEN",
            order_type="LIMIT",
            commission_type="BY_VOLUME",
            commission_parameter=Decimal("1"),
            commission_contract_multiplier=Decimal("10"),
            limit_price=Decimal("120"),
            total_volume=1,
            traded_volume=0,
            remaining_volume=1,
            cancelled_volume=0,
            status="ACCEPTED",
            submit_status="ACCEPTED",
            frozen_margin=Decimal("100"),
            frozen_cash=Decimal("0"),
            frozen_commission=Decimal("1"),
            frozen_position_volume=0,
        )
        db.add_all(
            [
                expired,
                live,
                *positions,
                *details,
                *trades,
                order,
                MarginRuleDaily(
                    order_book_id=future_code,
                    symbol=future_code,
                    exchange_id="ITDS",
                    trading_day=trading_day,
                    long_margin_rate=Decimal("0.10"),
                    short_margin_rate=Decimal("0.12"),
                    data_source="INTERNAL",
                ),
            ]
        )
        session_end = (now - timedelta(minutes=1)).astimezone(SHANGHAI)
        db.execute(
            text(
                "INSERT INTO trading_calendar "
                "(exchange_id, trading_day, previous_trading_day, next_trading_day, "
                "is_open, status) VALUES "
                "(:exchange, :day, :previous, :next, true, 'OPEN')"
            ),
            {
                "exchange": "ITDS",
                "day": trading_day,
                "previous": trading_day - timedelta(days=1),
                "next": next_day,
            },
        )
        for instrument_type, representative in (
            ("FUTURES", future_code),
            ("FUTURES_OPTION", expired_code),
        ):
            db.execute(
                text(
                    "INSERT INTO product_trading_schedule "
                    "(trading_day, exchange_id, product_code, instrument_type, "
                    "sessions, status, representative_order_book_id) VALUES "
                    "(:day, 'ITDS', 'ITP', :type, CAST(:sessions AS jsonb), "
                    "'READY', :representative)"
                ),
                {
                    "day": trading_day,
                    "type": instrument_type,
                    "sessions": json.dumps(
                        [
                            {
                                "start_at": (
                                    session_end - timedelta(hours=1)
                                ).isoformat(),
                                "end_at": session_end.isoformat(),
                            }
                        ]
                    ),
                    "representative": representative,
                },
            )
        db.commit()

    for sequence, (code, price) in enumerate(
        (
            (future_code, Decimal("120")),
            (expired_code, Decimal("7")),
            (live_code, Decimal("8")),
        ),
        start=1,
    ):
        _publish_tick(
            tick_store,
            code=code,
            trading_day=trading_day,
            now=now,
            price=price,
            sequence=sequence,
        )

    def execute():
        return DailySettlementService(
            session_factory=factory,
            database_engine=isolated_engine,
            tick_store=tick_store,
            time_provider=lambda: now,
            redis_recovery_enabled=False,
        ).run(trading_day)

    sql_statements = []

    def count_sql(_connection, _cursor, statement, _parameters, _context, _many):
        sql_statements.append(statement)

    event.listen(isolated_engine, "before_cursor_execute", count_sql)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: execute(), range(2)))

        assert sorted(item.already_completed for item in results) == [False, True]
        with factory() as db:
            account = db.scalar(select(Account).where(Account.account_id == account_id))
            order = db.scalar(select(Order))
            batch = db.scalar(select(DailySettlementBatch))
            positions = db.scalars(select(Position).order_by(Position.position_id)).all()

            assert batch.status == "COMPLETED"
            assert account.cash_balance == Decimal("10700.000000")
            assert account.equity == Decimal("10780.000000")
            assert account.trading_day == next_day
            assert order.status == "CANCELLED"
            assert order.remaining_volume == 0
            assert order.frozen_margin == Decimal("0.000000")
            assert len(db.scalars(select(InstrumentSettlementPrice)).all()) == 3
            assert len(db.scalars(select(DailyAccountSettlement)).all()) == 1
            assert len(db.scalars(select(DailyPositionSettlement)).all()) == 3
            assert len(db.scalars(select(OptionExpirySettlementDetail)).all()) == 1
            assert len(db.scalars(select(PositionDetail)).all()) == 4
            assert len(db.scalars(select(OutboxEvent)).all()) >= 6
            assert sum(item.total_volume for item in positions) == 3
            assert all(item.today_volume == 0 for item in positions)
        # 包含首次完整结算和并发等待后的幂等复跑；参考数据、规则和持仓均
        # 批量读取，不随账户内持仓数产生查询型 N+1。
        assert len(sql_statements) <= 100
        print(f"DAILY_SETTLEMENT_SQL_COUNT={len(sql_statements)}")
    finally:
        event.remove(isolated_engine, "before_cursor_execute", count_sql)
        pipeline = redis_client.pipeline(transaction=False)
        for code in (future_code, expired_code, live_code):
            pipeline.delete(market_latest_key("ITDS", code))
        pipeline.delete(stream_name)
        pipeline.execute()


@pytest.mark.integration
def test_real_postgres_account_failure_rolls_back_all_account_facts(
    isolated_settlement_database,
):
    isolated_engine, factory = isolated_settlement_database
    now = datetime.now(timezone.utc)
    trading_day = now.astimezone(SHANGHAI).date()
    next_day = trading_day + timedelta(days=1)
    account_ids = ("A-RESUME-1", "A-RESUME-2")

    with factory() as db:
        for index, account_id in enumerate(account_ids, start=1):
            user_id = f"U-RESUME-{index}"
            db.add(
                AppUser(
                    user_id=user_id,
                    username=f"resume_{index}",
                    password_hash="!",
                    display_name="恢复测试",
                )
            )
            db.flush()
            account = _account(account_id, user_id, trading_day)
            account.available_cash = Decimal("10000")
            account.risk_available_cash = Decimal("10000")
            account.used_margin = Decimal("0")
            account.frozen_margin = Decimal("0")
            account.frozen_commission = Decimal("0")
            db.add(account)
        db.execute(
            text(
                "INSERT INTO trading_calendar "
                "(exchange_id, trading_day, previous_trading_day, next_trading_day, "
                "is_open, status) VALUES "
                "('ITDS', :day, :previous, :next, true, 'OPEN')"
            ),
            {
                "day": trading_day,
                "previous": trading_day - timedelta(days=1),
                "next": next_day,
            },
        )
        db.commit()

    class FailBatchOnce(DailySettlementService):
        def _settle_batch_from_facts(self, **kwargs):
            raise RuntimeError("INJECTED_ATOMIC_BATCH_FAILURE")

    with pytest.raises(DailySettlementError) as raised:
        FailBatchOnce(
            session_factory=factory,
            database_engine=isolated_engine,
            time_provider=lambda: now,
            redis_recovery_enabled=False,
        ).run(trading_day)
    assert raised.value.account_id is None

    with factory() as db:
        batch = db.scalar(select(DailySettlementBatch))
        assert batch.status == "FAILED"
        assert db.scalar(
            select(DailyAccountSettlement)
        ) is None

    result = DailySettlementService(
        session_factory=factory,
        database_engine=isolated_engine,
        time_provider=lambda: now,
        redis_recovery_enabled=False,
    ).run(trading_day)

    assert result.status == "COMPLETED"
    with factory() as db:
        rows = db.scalars(
            select(DailyAccountSettlement).order_by(
                DailyAccountSettlement.account_id
            )
        ).all()
        assert len(rows) == 2
        assert all(item.status == "COMPLETED" for item in rows)
        assert all(item.cash_balance_after == Decimal("10000.000000") for item in rows)
