from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.enums.account_enums import AccountType
from app.enums.instrument_enums import InstrumentType
from app.enums.product_enums import ProductFamily
from app.enums.reference_data_enums import (
    StockDailyTradingFactUpsertResult,
    StockPriceLimitType,
)
from app.models.account import Account
from app.models.app_user import AppUser
from app.models.instrument import Instrument
from app.models.order import Order
from app.models.position import Position
from app.models.stock_daily_trading_fact import StockDailyTradingFact
from app.models.stock_trading_rule import StockTradingRule
from app.models.trade import Trade
from app.repositories.stock_daily_trading_fact_repository import (
    StockDailyTradingFactRepository,
)
from app.repositories.stock_trading_rule_repository import (
    StockTradingRuleRepository,
)
from app.repositories.account_repository import AccountRepository
from app.schemas.account_schema import AccountCreate
from app.schemas.instrument_schema import InstrumentCreate
from app.schemas.stock_daily_trading_fact_schema import (
    StockDailyTradingFactUpsert,
)
from app.schemas.stock_trading_rule_schema import StockTradingRuleCreate
from app.services.account_service import AccountService


NOW = datetime(2026, 8, 17, tzinfo=timezone.utc)


def _factory(*tables):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    for table in tables:
        table.create(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _stock_instrument(**overrides):
    values = {
        "order_book_id": "600519.XSHG",
        "symbol": "600519",
        "exchange_id": "SSE",
        "instrument_name": "贵州茅台",
        "market_type": "STOCK",
        "instrument_type": "STOCK",
        "contract_multiplier": Decimal("1"),
        "price_tick": Decimal("0.01"),
        "min_volume": 1,
        "max_volume": 1_000_000,
        "listed_date": date(2001, 8, 27),
        "is_active": True,
        "is_tradeable": True,
        "data_source": "TEST",
    }
    values.update(overrides)
    return Instrument(**values)


def _stock_rule(instrument_id: int, **overrides):
    values = {
        "instrument_id": instrument_id,
        "buy_lot_size": 100,
        "buy_volume_must_be_multiple": True,
        "sell_min_unit": 1,
        "sell_odd_lot_allowed": True,
        "settlement_days": 1,
        "price_limit_type": "RATIO",
        "normal_price_limit_ratio": Decimal("0.10"),
        "special_price_limit_ratio": Decimal("0.05"),
        "price_cage_enabled": False,
        "rule_version": "V1",
        "effective_from": date(2026, 1, 1),
        "effective_to": None,
        "data_source": "TEST",
    }
    values.update(overrides)
    return StockTradingRule(**values)


def _daily_fact(instrument_id: int, **overrides):
    values = {
        "instrument_id": instrument_id,
        "trading_day": date(2026, 8, 17),
        "previous_close": Decimal("100.00"),
        "upper_limit_price": Decimal("110.00"),
        "lower_limit_price": Decimal("90.00"),
        "is_suspended": False,
        "is_special_treatment": False,
        "is_tradeable": True,
        "source_event_id": "FACT-1",
        "data_source": "TEST",
        "synced_at": NOW,
    }
    values.update(overrides)
    return StockDailyTradingFact(**values)


def test_stock_enums_and_instrument_schema_are_supported():
    assert AccountType.STOCK.value == "STOCK"
    assert InstrumentType.STOCK.value == "STOCK"
    assert ProductFamily.STOCKS.value == "STOCKS"

    item = InstrumentCreate(
        order_book_id="600519.XSHG",
        symbol="600519",
        exchange_id="SSE",
        market_type="STOCK",
        instrument_type="STOCK",
        contract_multiplier=Decimal("1"),
        price_tick=Decimal("0.01"),
    )
    assert item.instrument_type is InstrumentType.STOCK
    assert item.price_tick == Decimal("0.01")

    with pytest.raises(ValueError, match="contract_multiplier"):
        InstrumentCreate(
            order_book_id="600519.XSHG",
            symbol="600519",
            exchange_id="SSE",
            market_type="STOCK",
            instrument_type="STOCK",
            contract_multiplier=Decimal("10"),
            price_tick=Decimal("0.01"),
        )


def test_stock_instrument_database_constraints_and_account_defaults():
    factory = _factory(Instrument.__table__, Account.__table__)
    with factory() as db:
        db.add(_stock_instrument())
        db.add(
            Account(
                account_id="STOCK-A",
                user_id="STOCK-U",
                account_name="股票账户",
                account_type="STOCK",
                initial_cash=Decimal("100000"),
                cash_balance=Decimal("100000"),
                available_cash=Decimal("100000"),
                frozen_cash=Decimal("0"),
                equity=Decimal("100000"),
                used_margin=Decimal("0"),
                frozen_margin=Decimal("0"),
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                daily_pnl=Decimal("0"),
                used_commission=Decimal("0"),
                frozen_commission=Decimal("0"),
                risk_ratio=Decimal("0"),
                status="NORMAL",
            )
        )
        db.commit()
        account = db.get(Account, 1)
        assert account.stock_market_value == Decimal("0.000000")

        db.add(_stock_instrument(symbol="600520", order_book_id="600520.XSHG", contract_multiplier=Decimal("10")))
        with pytest.raises(IntegrityError):
            db.commit()


def test_account_service_creates_stock_account_with_zero_market_value():
    factory = _factory(AppUser.__table__, Account.__table__)
    with factory() as db:
        db.add(
            AppUser(
                user_id="STOCK-U",
                username="stock_user",
                password_hash="!",
                display_name="股票用户",
                role="USER",
                status="ACTIVE",
            )
        )
        db.commit()
        account = AccountService(AccountRepository()).create_account(
            db,
            AccountCreate(
                account_id="STOCK-SERVICE-A",
                user_id="STOCK-U",
                account_name="股票账户",
                account_type=AccountType.STOCK,
                initial_cash=Decimal("100000"),
            ),
        )
        assert account.account_type == "STOCK"
        assert account.stock_market_value == Decimal("0.000000")


def test_stock_rule_repository_resolves_exactly_one_effective_rule():
    factory = _factory(Instrument.__table__, StockTradingRule.__table__)
    with factory() as db:
        instrument = _stock_instrument()
        db.add(instrument)
        db.flush()
        rule = _stock_rule(instrument.id)
        StockTradingRuleRepository.create(db, rule)
        db.commit()

        resolved = StockTradingRuleRepository.resolve_for_trading_day(
            db,
            instrument_id=instrument.id,
            trading_day=date(2026, 8, 17),
        )
        assert resolved.rule_version == "V1"
        assert resolved.normal_price_limit_ratio == Decimal("0.10000000")

        with pytest.raises(ValueError, match="有效期不能重叠"):
            StockTradingRuleRepository.create(
                db,
                _stock_rule(
                    instrument.id,
                    rule_version="V2",
                    effective_from=date(2026, 8, 1),
                ),
            )

        db.add(
            _stock_rule(
                instrument.id,
                rule_version="V3",
                effective_from=date(2026, 8, 1),
            )
        )
        db.commit()
        with pytest.raises(LookupError, match="有效期冲突"):
            StockTradingRuleRepository.resolve_for_trading_day(
                db,
                instrument_id=instrument.id,
                trading_day=date(2026, 8, 17),
            )


def test_stock_rule_repository_locks_only_the_target_stock_instrument():
    db = Mock()

    StockTradingRuleRepository.get_stock_instrument_for_update(
        db,
        instrument_id=42,
    )

    statement = db.scalar.call_args.args[0]
    compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert "FOR UPDATE" in compiled
    assert "instrument.id = 42" in compiled


def test_stock_rule_schema_rejects_zero_limits_and_unknown_limit_type():
    values = {
        "instrument_id": 1,
        "buy_lot_size": 100,
        "sell_min_unit": 1,
        "settlement_days": 1,
        "price_limit_type": StockPriceLimitType.RATIO,
        "rule_version": "V1",
        "effective_from": date(2026, 8, 17),
        "data_source": "TEST",
    }
    with pytest.raises(ValueError, match="greater than 0"):
        StockTradingRuleCreate(
            **values,
            normal_price_limit_ratio=Decimal("0"),
        )
    with pytest.raises(ValueError):
        StockTradingRuleCreate(
            **values,
            price_limit_type="UNKNOWN",
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "upper_limit_price": Decimal("90"),
            "lower_limit_price": Decimal("100"),
        },
        {"is_suspended": True, "is_tradeable": True},
    ],
)
def test_stock_daily_fact_schema_rejects_inconsistent_states(overrides):
    values = {
        "instrument_id": 1,
        "trading_day": date(2026, 8, 17),
        "previous_close": Decimal("100"),
        "upper_limit_price": Decimal("110"),
        "lower_limit_price": Decimal("90"),
        "is_suspended": False,
        "is_special_treatment": False,
        "is_tradeable": True,
        "source_event_id": "FACT-1",
        "data_source": "TEST",
        "synced_at": NOW,
    }
    values.update(overrides)
    with pytest.raises(ValueError):
        StockDailyTradingFactUpsert(**values)


def test_stock_daily_fact_constraints_and_batch_lookup_are_supported():
    factory = _factory(Instrument.__table__, StockDailyTradingFact.__table__)
    with factory() as db:
        first = _stock_instrument()
        second = _stock_instrument(
            order_book_id="600520.XSHG", symbol="600520"
        )
        db.add_all([first, second])
        db.flush()
        db.add_all([
            _daily_fact(first.id),
            _daily_fact(second.id, source_event_id="FACT-2"),
        ])
        db.commit()

        rows = StockDailyTradingFactRepository.list_by_instrument_ids_and_trading_day(
            db,
            instrument_ids=[second.id, first.id],
            trading_day=date(2026, 8, 17),
        )
        assert [row.instrument_id for row in rows] == [first.id, second.id]
        assert rows[0].upper_limit_price == Decimal("110.000000")

        db.add(_daily_fact(first.id, source_event_id="DUPLICATE"))
        with pytest.raises(IntegrityError):
            db.commit()


def test_stock_reference_models_reject_database_inconsistent_states():
    rule_factory = _factory(Instrument.__table__, StockTradingRule.__table__)
    with rule_factory() as db:
        db.add(_stock_rule(1, normal_price_limit_ratio=Decimal("0")))
        with pytest.raises(IntegrityError):
            db.commit()

    fact_factory = _factory(
        Instrument.__table__, StockDailyTradingFact.__table__
    )
    with fact_factory() as db:
        db.add(
            _daily_fact(
                1,
                upper_limit_price=Decimal("90"),
                lower_limit_price=Decimal("100"),
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()

    with fact_factory() as db:
        db.add(_daily_fact(1, is_suspended=True, is_tradeable=True))
        with pytest.raises(IntegrityError):
            db.commit()


@pytest.mark.parametrize("instrument_type", ["FUTURES", "FUTURES_OPTION"])
def test_stock_daily_fact_repository_rejects_non_stock_instrument(
    instrument_type,
):
    db = Mock()
    db.get.return_value = SimpleNamespace(instrument_type=instrument_type)

    with pytest.raises(ValueError, match="STOCK Instrument"):
        StockDailyTradingFactRepository.upsert(
            db,
            instrument_id=1,
            trading_day=date(2026, 8, 17),
            previous_close=Decimal("100"),
            upper_limit_price=Decimal("110"),
            lower_limit_price=Decimal("90"),
            is_suspended=False,
            is_special_treatment=False,
            is_tradeable=True,
            source_event_id="FACT-1",
            data_source="TEST",
            synced_at=NOW,
            updated_at=NOW,
        )


def test_stock_daily_fact_repository_classifies_duplicate_and_stale_events():
    current = SimpleNamespace(
        source_event_id="FACT-NEW",
        synced_at=datetime(2026, 8, 17, 10, tzinfo=timezone.utc),
    )
    db = Mock()
    db.get.return_value = SimpleNamespace(instrument_type="STOCK")
    db.scalar.side_effect = [None, current]

    result = StockDailyTradingFactRepository.upsert(
        db,
        instrument_id=1,
        trading_day=date(2026, 8, 17),
        previous_close=Decimal("100"),
        upper_limit_price=Decimal("110"),
        lower_limit_price=Decimal("90"),
        is_suspended=False,
        is_special_treatment=False,
        is_tradeable=True,
        source_event_id="FACT-OLD",
        data_source="TEST",
        synced_at=datetime(2026, 8, 17, 9, tzinfo=timezone.utc),
        updated_at=NOW,
    )

    assert result == StockDailyTradingFactUpsertResult.IGNORED_STALE

    db.scalar.side_effect = [None, current]
    result = StockDailyTradingFactRepository.upsert(
        db,
        instrument_id=1,
        trading_day=date(2026, 8, 17),
        previous_close=Decimal("100"),
        upper_limit_price=Decimal("110"),
        lower_limit_price=Decimal("90"),
        is_suspended=False,
        is_special_treatment=False,
        is_tradeable=True,
        source_event_id="FACT-NEW",
        data_source="TEST",
        synced_at=datetime(2026, 8, 17, 11, tzinfo=timezone.utc),
        updated_at=NOW,
    )

    assert result == StockDailyTradingFactUpsertResult.DUPLICATE


def test_stock_daily_fact_repository_classifies_insert_update_and_tie():
    db = Mock()
    db.get.return_value = SimpleNamespace(instrument_type="STOCK")
    db.scalar.return_value = 99

    inserted = StockDailyTradingFactRepository.upsert(
        db,
        instrument_id=1,
        trading_day=date(2026, 8, 17),
        previous_close=Decimal("100"),
        upper_limit_price=Decimal("110"),
        lower_limit_price=Decimal("90"),
        is_suspended=False,
        is_special_treatment=False,
        is_tradeable=True,
        source_event_id="FACT-INSERT",
        data_source="TEST",
        synced_at=NOW,
        updated_at=NOW,
    )
    assert inserted == StockDailyTradingFactUpsertResult.INSERTED

    current = SimpleNamespace(
        previous_close=Decimal("100"),
        upper_limit_price=Decimal("110"),
        lower_limit_price=Decimal("90"),
        is_suspended=False,
        is_special_treatment=False,
        is_tradeable=True,
        source_event_id="FACT-OLD",
        data_source="OLD",
        synced_at=datetime(2026, 8, 17, 10, tzinfo=timezone.utc),
        updated_at=NOW,
    )
    db.scalar.side_effect = [None, current]
    same_time = StockDailyTradingFactRepository.upsert(
        db,
        instrument_id=1,
        trading_day=date(2026, 8, 17),
        previous_close=Decimal("101"),
        upper_limit_price=Decimal("111"),
        lower_limit_price=Decimal("91"),
        is_suspended=False,
        is_special_treatment=False,
        is_tradeable=True,
        source_event_id="FACT-TIE",
        data_source="TEST",
        synced_at=current.synced_at,
        updated_at=NOW,
    )
    assert same_time == StockDailyTradingFactUpsertResult.CONFLICT_SAME_TIMESTAMP
    assert current.source_event_id == "FACT-OLD"

    newer_at = datetime(2026, 8, 17, 11, tzinfo=timezone.utc)
    db.scalar.side_effect = [None, current]
    updated = StockDailyTradingFactRepository.upsert(
        db,
        instrument_id=1,
        trading_day=date(2026, 8, 17),
        previous_close=Decimal("101"),
        upper_limit_price=Decimal("111"),
        lower_limit_price=Decimal("91"),
        is_suspended=False,
        is_special_treatment=True,
        is_tradeable=True,
        source_event_id="FACT-NEW",
        data_source="TEST",
        synced_at=newer_at,
        updated_at=NOW,
    )
    assert updated == StockDailyTradingFactUpsertResult.UPDATED
    assert current.source_event_id == "FACT-NEW"
    assert current.synced_at == newer_at


def test_position_and_order_trade_stock_offset_constraints():
    factory = _factory(Position.__table__, Order.__table__, Trade.__table__)
    with factory() as db:
        db.add(
            Position(
                position_id="P-STOCK",
                account_id="A-STOCK",
                order_book_id="600519.XSHG",
                exchange_id="SSE",
                symbol="600519",
                instrument_type="STOCK",
                direction="LONG",
                total_volume=100,
                today_volume=100,
                yesterday_volume=0,
                frozen_volume=20,
                settlement_locked_volume=30,
                available_volume=50,
                average_open_price=Decimal("100"),
                position_cost=Decimal("10000"),
                used_margin=Decimal("0"),
                multiplier_snapshot=Decimal("1"),
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                daily_position_pnl=Decimal("0"),
                daily_close_pnl=Decimal("0"),
                trading_day=date(2026, 8, 17),
            )
        )
        db.commit()

        db.add(
            Position(
                position_id="P-BAD",
                account_id="A-STOCK",
                order_book_id="600520.XSHG",
                exchange_id="SSE",
                symbol="600520",
                instrument_type="STOCK",
                direction="LONG",
                total_volume=100,
                today_volume=100,
                yesterday_volume=0,
                frozen_volume=20,
                settlement_locked_volume=30,
                available_volume=80,
                average_open_price=Decimal("100"),
                position_cost=Decimal("10000"),
                used_margin=Decimal("0"),
                multiplier_snapshot=Decimal("1"),
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                daily_position_pnl=Decimal("0"),
                daily_close_pnl=Decimal("0"),
                trading_day=date(2026, 8, 17),
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()

    order_factory = _factory(Order.__table__)
    with order_factory() as db:
        valid = _order("O-STOCK", instrument_type="STOCK", offset_flag=None)
        db.add(valid)
        db.commit()
        db.add(_order("O-BAD", instrument_type="STOCK", offset_flag="OPEN"))
        with pytest.raises(IntegrityError):
            db.commit()

    trade_factory = _factory(Trade.__table__)
    with trade_factory() as db:
        db.add(_trade("T-STOCK", instrument_type="STOCK", offset_flag=None))
        db.commit()
        db.add(_trade("T-BAD", instrument_type="FUTURES", offset_flag=None))
        with pytest.raises(IntegrityError):
            db.commit()


def _order(order_id: str, **overrides) -> Order:
    values = {
        "order_id": order_id,
        "client_order_id": f"C-{order_id}",
        "account_id": "A-STOCK",
        "order_book_id": "600519.XSHG",
        "symbol": "600519",
        "exchange_id": "SSE",
        "trading_day": date(2026, 8, 17),
        "instrument_type": "FUTURES",
        "direction": "BUY",
        "offset_flag": "OPEN",
        "order_type": "LIMIT",
        "commission_type": "BY_VOLUME",
        "commission_parameter": Decimal("0"),
        "commission_contract_multiplier": Decimal("1"),
        "limit_price": Decimal("100"),
        "total_volume": 1,
        "traded_volume": 0,
        "remaining_volume": 1,
        "cancelled_volume": 0,
        "status": "ACCEPTED",
        "submit_status": "ACCEPTED",
        "frozen_margin": Decimal("0"),
        "frozen_cash": Decimal("0"),
        "frozen_commission": Decimal("0"),
        "frozen_position_volume": 0,
    }
    values.update(overrides)
    return Order(**values)


def _trade(trade_id: str, **overrides) -> Trade:
    values = {
        "trade_id": trade_id,
        "order_id": f"O-{trade_id}",
        "account_id": "A-STOCK",
        "market_event_id": f"E-{trade_id}",
        "market_stream_message_id": f"S-{trade_id}",
        "order_book_id": "600519.XSHG",
        "exchange_id": "SSE",
        "symbol": "600519",
        "trading_day": date(2026, 8, 17),
        "instrument_type": "FUTURES",
        "direction": "BUY",
        "offset_flag": "OPEN",
        "trade_price": Decimal("100"),
        "trade_volume": 1,
        "turnover": Decimal("100"),
        "margin": Decimal("0"),
        "premium_cash_flow": Decimal("0"),
        "commission": Decimal("0"),
        "realized_pnl": Decimal("0"),
        "daily_close_pnl": Decimal("0"),
        "trade_time": NOW,
    }
    values.update(overrides)
    return Trade(**values)
