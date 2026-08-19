from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.common.exceptions import BusinessRuleError
from app.models.account import Account
from app.models.app_user import AppUser
from app.models.cash_security_corporate_action_entitlement import (
    CashSecurityCorporateActionEntitlement,
)
from app.models.cash_security_corporate_action_ledger import (
    CashSecurityCorporateActionLedger,
)
from app.models.cash_security_corporate_action_position_adjustment import (
    CashSecurityCorporateActionPositionAdjustment,
)
from app.models.cash_security_corporate_action_subscription import (
    CashSecurityCorporateActionSubscription,
)
from app.models.instrument import Instrument
from app.models.position import Position
from app.services.account_access_scope import AccountAccessScope
from app.services.cash_security_corporate_action_service import (
    CashSecurityCorporateActionService,
)
from app.services.cash_security_valuation_service import CashSecurityValuationService
from app.services.cash_security_price_adjustment_service import (
    CashSecurityPriceAdjustmentService,
)
from app.services.cash_security_historical_price_query_service import (
    CashSecurityHistoricalPriceQueryService,
)
from app.services.daily_settlement_service import DailySettlementService
from app.schemas.market_tick_schema import MarketTick, MarketTickIngestType
from app.infrastructure.market_data.market_tick_store import MarketTickStore


DAY = date(2026, 8, 20)


class _TickStore:
    def __init__(self, mapping):
        self.mapping = mapping

    def get_latest_many(self, _keys):
        return self.mapping


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        engine.dispose()


def _seed_cash_position(db, *, instrument_type="STOCK", volume=1000):
    db.add(AppUser(user_id="U-CA", username="u_ca", password_hash="x", display_name="CA", role="USER", status="ACTIVE"))
    db.add(Account(account_id="A-CA", user_id="U-CA", account_name="CA", account_type="SECURITIES_CASH", initial_cash=Decimal("100000"), cash_balance=Decimal("100000"), available_cash=Decimal("100000"), frozen_cash=Decimal("0"), equity=Decimal("100000"), used_margin=Decimal("0"), frozen_margin=Decimal("0"), realized_pnl=Decimal("0"), unrealized_pnl=Decimal("0"), daily_pnl=Decimal("0"), used_commission=Decimal("0"), frozen_commission=Decimal("0"), risk_ratio=Decimal("0"), status="NORMAL", trading_day=DAY))
    instrument = Instrument(order_book_id=f"CA-{instrument_type}", symbol=f"CA-{instrument_type}", exchange_id="TEST", instrument_name="CA", product_id="CA", market_type="STOCK" if instrument_type == "STOCK" else "BOND", instrument_type=instrument_type, contract_multiplier=Decimal("1"), price_tick=Decimal("0.01"), min_volume=1, max_volume=100000, is_active=True, is_tradeable=True, data_source="TEST")
    db.add(instrument)
    db.flush()
    db.add(Position(position_id="P-CA", account_id="A-CA", order_book_id=instrument.order_book_id, exchange_id="TEST", symbol=instrument.symbol, instrument_type=instrument_type, direction="LONG", total_volume=volume, today_volume=0, yesterday_volume=volume, frozen_volume=0, settlement_locked_volume=0, available_volume=volume, average_open_price=Decimal("10"), position_cost=Decimal(volume * 10), used_margin=Decimal("0"), multiplier_snapshot=Decimal("1"), realized_pnl=Decimal("0"), unrealized_pnl=Decimal("0"), daily_position_pnl=Decimal("0"), daily_close_pnl=Decimal("0"), trading_day=DAY, daily_pnl_base_cost=Decimal(volume * 10), yesterday_pnl_base_cost=Decimal(volume * 10), today_pnl_base_cost=Decimal("0"), daily_pnl_base_established=True))
    db.flush()
    return instrument


def test_entitlement_units_uses_integer_floor_and_retains_fraction_for_audit():
    whole, fraction = CashSecurityCorporateActionService._units(
        101, Decimal("10"), Decimal("3")
    )

    assert whole == 30
    assert fraction == Decimal("0.3")


def test_entitlement_units_does_not_use_float_for_large_quantities():
    whole, fraction = CashSecurityCorporateActionService._units(
        9_999_999, Decimal("10"), Decimal("1.5")
    )

    assert whole == 1_499_999
    assert fraction == Decimal("0.85")


def test_cash_dividend_and_stock_dividend_run_from_record_to_payment_and_listing(session_factory):
    """记录日快照不受后续卖出影响，派息和送股各只执行一次。"""
    with session_factory() as db:
        instrument = _seed_cash_position(db)
        service = CashSecurityCorporateActionService()
        action = service.import_action(db, payload={"instrument_id": instrument.id, "source_action_id": "DIV-1", "action_version": 7, "data_source": "TEST", "record_date": DAY, "ex_date": DAY, "payment_date": DAY, "listing_date": DAY}, components=[
            {"component_type": "CASH_DIVIDEND", "base_quantity": "10", "cash_amount": "2"},
            {"component_type": "STOCK_DIVIDEND", "base_quantity": "10", "share_ratio": "1"},
        ])
        service.confirm(db, action_id=action.action_id)
        service.capture_entitlements(db, action_id=action.action_id, trading_day=DAY)
        service.apply_ex_date(db, action_id=action.action_id, trading_day=DAY)
        service.pay_cash(db, action_id=action.action_id, trading_day=DAY)
        service.list_pending_shares(db, action_id=action.action_id, trading_day=DAY)
        db.commit()

        account = db.get(Account, 1)
        position = db.scalar(select(Position).where(Position.position_id == "P-CA"))
        entitlements = db.scalars(select(CashSecurityCorporateActionEntitlement).order_by(CashSecurityCorporateActionEntitlement.id)).all()
        assert [row.entitled_cash_net for row in entitlements] == [Decimal("200.000000"), Decimal("0.000000")]
        assert position.total_volume == position.available_volume == position.yesterday_volume == 1100
        assert position.pending_share_volume == 0
        assert account.cash_balance == account.available_cash == Decimal("100200.000000")
        assert account.corporate_action_receivable == Decimal("0.000000")
        ledgers = db.scalars(select(CashSecurityCorporateActionLedger)).all()
        assert ledgers
        assert {row.business_version for row in ledgers} == {"7"}
        adjustments = db.scalars(
            select(CashSecurityCorporateActionPositionAdjustment)
        ).all()
        assert [(row.adjustment_type, row.pending_volume_delta) for row in adjustments] == [
            ("SHARES_PENDING", 100),
            ("SHARES_LISTED", -100),
        ]
        assert adjustments[-1].total_volume_delta == 100
        assert {row.business_version for row in adjustments} == {"7"}


def test_rights_subscription_is_authorized_idempotent_and_creates_pending_shares(session_factory):
    with session_factory() as db:
        instrument = _seed_cash_position(db)
        service = CashSecurityCorporateActionService()
        action = service.import_action(db, payload={"instrument_id": instrument.id, "source_action_id": "RIGHTS-1", "data_source": "TEST", "record_date": DAY, "subscription_start_date": DAY, "subscription_end_date": DAY}, components=[{"component_type": "RIGHTS_ISSUE", "base_quantity": "10", "rights_ratio": "3", "subscription_price": "8"}])
        service.confirm(db, action_id=action.action_id)
        service.capture_entitlements(db, action_id=action.action_id, trading_day=DAY)
        first = service.subscribe_rights(db, action_id=action.action_id, account_id="A-CA", volume=100, client_request_id="REQ-1", access_scope=AccountAccessScope.for_user("U-CA"), trading_day=DAY)
        second = service.subscribe_rights(db, action_id=action.action_id, account_id="A-CA", volume=100, client_request_id="REQ-2", access_scope=AccountAccessScope.for_user("U-CA"), trading_day=DAY)
        again = service.subscribe_rights(db, action_id=action.action_id, account_id="A-CA", volume=100, client_request_id="REQ-1", access_scope=AccountAccessScope.for_user("U-CA"), trading_day=DAY)
        db.commit()

        account = db.get(Account, 1)
        position = db.scalar(select(Position).where(Position.position_id == "P-CA"))
        assert again.entitlement_id == first.entitlement_id
        assert first.entitled_share_volume == 300
        assert second.entitlement_id == first.entitlement_id
        assert first.subscribed_volume == first.pending_share_volume == 200
        assert first.status == "PARTIALLY_SUBSCRIBED"
        assert account.cash_balance == account.available_cash == Decimal("98400.000000")
        assert account.pending_security_value == Decimal("1600.000000")
        assert position.pending_share_volume == 200
        assert len(db.scalars(select(CashSecurityCorporateActionSubscription)).all()) == 2


def test_stock_split_changes_quantities_but_not_total_cost_or_daily_basis(session_factory):
    with session_factory() as db:
        instrument = _seed_cash_position(db)
        service = CashSecurityCorporateActionService()
        action = service.import_action(db, payload={"instrument_id": instrument.id, "source_action_id": "SPLIT-1", "data_source": "TEST", "record_date": DAY, "ex_date": DAY}, components=[{"component_type": "STOCK_SPLIT", "base_quantity": "1", "share_ratio": "2"}])
        service.confirm(db, action_id=action.action_id)
        service.capture_entitlements(db, action_id=action.action_id, trading_day=DAY)
        service.apply_ex_date(db, action_id=action.action_id, trading_day=DAY)
        db.commit()

        position = db.scalar(select(Position).where(Position.position_id == "P-CA"))
        assert position.total_volume == position.yesterday_volume == position.available_volume == 2000
        assert position.position_cost == Decimal("10000.000000")
        assert position.average_open_price == Decimal("5.000000")
        assert position.daily_pnl_base_cost == position.yesterday_pnl_base_cost == Decimal("10000.000000")
        adjustment = db.scalar(select(CashSecurityCorporateActionPositionAdjustment))
        assert adjustment.adjustment_type == "STOCK_SPLIT"
        assert adjustment.total_volume_delta == 1000
        assert adjustment.position_cost_delta == Decimal("0.000000")
        assert Decimal(adjustment.replay_payload["multiplier"]) == Decimal("2")


def test_new_source_revision_supersedes_unexecuted_revision(session_factory):
    with session_factory() as db:
        instrument = _seed_cash_position(db)
        service = CashSecurityCorporateActionService()
        v1 = service.import_action(
            db,
            payload={"instrument_id": instrument.id, "source_action_id": "REV-1", "action_version": 1, "data_source": "TEST", "record_date": DAY, "ex_date": DAY, "payment_date": DAY},
            components=[{"component_type": "CASH_DIVIDEND", "base_quantity": "10", "cash_amount": "1"}],
        )
        v2 = service.import_action(
            db,
            payload={"instrument_id": instrument.id, "source_action_id": "REV-1", "action_version": 2, "data_source": "TEST", "record_date": DAY, "ex_date": DAY, "payment_date": DAY},
            components=[{"component_type": "CASH_DIVIDEND", "base_quantity": "10", "cash_amount": "2"}],
        )
        assert v1.status == "SUPERSEDED"
        assert v1.superseded_by_action_id == v2.action_id
        with pytest.raises(BusinessRuleError, match="superseded"):
            service.confirm(db, action_id=v1.action_id)
        assert v1.status == "SUPERSEDED"
        service.confirm(db, action_id=v2.action_id)
        assert v2.status == "CONFIRMED"


def test_completed_action_is_not_scanned_or_emitted_again(session_factory):
    with session_factory() as db:
        instrument = _seed_cash_position(db)
        service = CashSecurityCorporateActionService()
        action = service.import_action(
            db,
            payload={"instrument_id": instrument.id, "source_action_id": "COMPLETE-1", "data_source": "TEST", "record_date": DAY, "ex_date": DAY, "payment_date": DAY},
            components=[{"component_type": "CASH_DIVIDEND", "base_quantity": "10", "cash_amount": "2"}],
        )
        service.confirm(db, action_id=action.action_id)
        assert service.run_due_actions(db, trading_day=DAY) == 3
        assert action.status == "COMPLETED"
        event_count = len(db.scalars(select(CashSecurityCorporateActionLedger)).all())
        assert service.run_due_actions(db, trading_day=DAY) == 0
        assert len(db.scalars(select(CashSecurityCorporateActionLedger)).all()) == event_count


def test_price_adjustment_service_applies_saved_factor_to_historical_ohlc(session_factory):
    with session_factory() as db:
        instrument = _seed_cash_position(db)
        service = CashSecurityCorporateActionService()
        action = service.import_action(
            db,
            payload={"instrument_id": instrument.id, "source_action_id": "FACTOR-1", "data_source": "TEST", "ex_date": DAY},
            components=[{"component_type": "CASH_DIVIDEND", "base_quantity": "10", "cash_amount": "1"}],
        )
        service.record_price_adjustment_factor(
            db,
            action_id=action.action_id,
            trading_day=DAY,
            raw_previous_close=Decimal("10"),
            official_ex_reference_price=Decimal("8.909090909"),
            source_event_id="OFFICIAL-EX-1",
            data_source="TEST",
        )
        adjusted = CashSecurityPriceAdjustmentService().adjust_bars(
            db,
            instrument_id=instrument.id,
            mode="FORWARD",
            bars=[
                {"trading_day": date(2026, 8, 19), "open": Decimal("10"), "high": Decimal("10"), "low": Decimal("10"), "close": Decimal("10")},
                {"trading_day": DAY, "open": Decimal("8.909090909"), "high": Decimal("8.909090909"), "low": Decimal("8.909090909"), "close": Decimal("8.909090909")},
            ],
        )
        assert adjusted[0]["close"] == Decimal("8.909090909")
        assert adjusted[1]["close"] == Decimal("8.909090909")


def test_historical_price_query_reads_raw_bars_then_applies_adjustment(session_factory):
    class Source:
        def fetch_daily_bars(self, order_book_id, *, start_date, end_date):
            assert order_book_id == "CA-STOCK"
            return [{
                "trading_day": date(2026, 8, 19), "open": Decimal("10"),
                "high": Decimal("10"), "low": Decimal("10"), "close": Decimal("10"),
            }]

    with session_factory() as db:
        instrument = _seed_cash_position(db)
        action = CashSecurityCorporateActionService().import_action(
            db,
            payload={"instrument_id": instrument.id, "source_action_id": "HISTORY-FACTOR", "data_source": "TEST", "ex_date": DAY},
            components=[{"component_type": "CASH_DIVIDEND", "base_quantity": "10", "cash_amount": "1"}],
        )
        CashSecurityCorporateActionService().record_price_adjustment_factor(
            db, action_id=action.action_id, trading_day=DAY,
            raw_previous_close=Decimal("10"), official_ex_reference_price=Decimal("8.909090909"),
            source_event_id="EX-REFERENCE", data_source="TEST",
        )
        bars = CashSecurityHistoricalPriceQueryService().query_daily_bars(
            db, source=Source(), order_book_id="CA-STOCK", start_date=date(2026, 8, 19),
            end_date=DAY, adjustment_mode="FORWARD",
        )
        assert bars[0]["close"] == Decimal("8.909090909")
        assert bars[0]["adjustment_mode"] == "FORWARD"


def test_convertible_bond_maturity_creates_principal_receivable_then_cash(session_factory):
    with session_factory() as db:
        instrument = _seed_cash_position(db, instrument_type="CONVERTIBLE_BOND", volume=100)
        service = CashSecurityCorporateActionService()
        action = service.import_action(db, payload={"instrument_id": instrument.id, "source_action_id": "MATURITY-1", "data_source": "TEST", "record_date": DAY, "ex_date": DAY, "payment_date": DAY}, components=[{"component_type": "BOND_MATURITY_REDEMPTION", "base_quantity": "1", "cash_amount": "103"}])
        service.confirm(db, action_id=action.action_id)
        service.capture_entitlements(db, action_id=action.action_id, trading_day=DAY)
        service.apply_ex_date(db, action_id=action.action_id, trading_day=DAY)
        service.apply_bond_maturity(db, action_id=action.action_id, trading_day=DAY)
        service.pay_cash(db, action_id=action.action_id, trading_day=DAY)
        db.commit()

        account = db.get(Account, 1)
        position = db.scalar(select(Position).where(Position.position_id == "P-CA"))
        assert position.total_volume == position.available_volume == 0
        assert position.position_cost == Decimal("0.000000")
        assert account.cash_balance == account.available_cash == Decimal("110300.000000")
        assert account.corporate_action_receivable == Decimal("0.000000")
        assert account.corporate_action_income == Decimal("0.000000")
        adjustment = db.scalar(select(CashSecurityCorporateActionPositionAdjustment))
        assert adjustment.adjustment_type == "BOND_MATURITY_RETIRED"
        assert adjustment.total_volume_delta == -100


def test_reverse_split_records_fractional_cash_in_lieu_instead_of_losing_tail(session_factory):
    with session_factory() as db:
        instrument = _seed_cash_position(db, volume=1003)
        service = CashSecurityCorporateActionService()
        action = service.import_action(db, payload={"instrument_id": instrument.id, "source_action_id": "MERGE-1", "data_source": "TEST", "record_date": DAY, "ex_date": DAY, "payment_date": DAY}, components=[{"component_type": "REVERSE_SPLIT", "base_quantity": "10", "share_ratio": "1", "cash_in_lieu_price": "5"}])
        service.confirm(db, action_id=action.action_id)
        service.capture_entitlements(db, action_id=action.action_id, trading_day=DAY)
        service.apply_ex_date(db, action_id=action.action_id, trading_day=DAY)
        service.pay_cash(db, action_id=action.action_id, trading_day=DAY)
        db.commit()

        position = db.scalar(select(Position).where(Position.position_id == "P-CA"))
        account = db.get(Account, 1)
        assert position.total_volume == 100
        assert account.corporate_action_receivable == Decimal("0.000000")
        assert account.cash_balance == Decimal("100001.500000")


def test_eod_orchestrator_runs_due_action_once_and_is_safe_to_retry(session_factory):
    with session_factory() as db:
        instrument = _seed_cash_position(db)
        service = CashSecurityCorporateActionService()
        action = service.import_action(db, payload={"instrument_id": instrument.id, "source_action_id": "EOD-1", "data_source": "TEST", "record_date": DAY, "ex_date": DAY, "payment_date": DAY}, components=[{"component_type": "CASH_DIVIDEND", "base_quantity": "10", "cash_amount": "2"}])
        service.confirm(db, action_id=action.action_id)
        assert service.run_due_actions(db, trading_day=DAY) == 3
        # The ledger idempotency key makes a rerun after a failed batch a no-op
        # for cash, even though the batch-level scheduler calls it again.
        service.run_due_actions(db, trading_day=DAY)
        db.commit()

        account = db.get(Account, 1)
        ledgers = db.scalars(select(CashSecurityCorporateActionLedger)).all()
        assert account.cash_balance == Decimal("100200.000000")
        assert len([row for row in ledgers if row.entry_type == "CASH_PAID"]) == 1


def test_daily_settlement_hook_reprices_existing_stock_after_dividend_and_stock_dividend(session_factory):
    """已有昨仓在日结屏障后执行公司行为，除权后权益口径仍连续。"""
    with session_factory() as db:
        instrument = _seed_cash_position(db)
        position = db.scalar(select(Position).where(Position.position_id == "P-CA"))
        # 上一交易日已结算的原始收盘价；送股待上市价值会在本次估值中重算。
        position.mark_price = Decimal("10")
        position.mark_time = datetime(2026, 8, 19, tzinfo=timezone.utc)
        position.mark_source_event_id = "PREVIOUS-CLOSE"
        service = CashSecurityCorporateActionService()
        action = service.import_action(db, payload={"instrument_id": instrument.id, "source_action_id": "DAILY-HOOK-1", "data_source": "TEST", "record_date": DAY, "ex_date": DAY}, components=[
            {"component_type": "CASH_DIVIDEND", "base_quantity": "10", "cash_amount": "2"},
            {"component_type": "STOCK_DIVIDEND", "base_quantity": "10", "share_ratio": "1"},
        ])
        service.confirm(db, action_id=action.action_id)
        db.commit()

    # This is the exact DailySettlementService hook, executed after the order
    # barrier; it is intentionally not a direct call to an action method.
    DailySettlementService(session_factory=session_factory)._apply_due_corporate_actions(DAY)
    # SQLite drops timezone information.  Clear the historical mark triple so
    # this isolated test does not exercise cross-dialect timestamp comparison.
    with session_factory() as db:
        position = db.scalar(select(Position).where(Position.position_id == "P-CA"))
        position.mark_price = position.mark_time = position.mark_source_event_id = None
        db.commit()

    tick = MarketTick(
        source_event_id="EX-TICK", ingest_type=MarketTickIngestType.LIVE_CALLBACK,
        order_book_id="CA-STOCK", exchange_id="TEST", symbol="CA-STOCK",
        trading_day=DAY, event_time=datetime.now(timezone.utc), sequence_id=1,
        last_price=Decimal("8.909090909"), cumulative_volume=1,
        bid_volume_1=1, ask_volume_1=1,
    )
    valuation = CashSecurityValuationService(
        session_factory=session_factory,
        store=Mock(),
        market_tick_store=_TickStore({("TEST", "CA-STOCK"): MarketTickStore.tick_to_mapping(tick)}),
        pnl_store=Mock(),
    )
    with session_factory() as db:
        account = db.scalar(select(Account).where(Account.account_id == "A-CA").with_for_update())
        complete, _, _, _ = valuation._recalculate_locked_account(db, account)
        db.commit()
        position = db.scalar(select(Position).where(Position.position_id == "P-CA"))

        assert complete is True
        assert position.total_volume == 1000
        assert position.pending_share_volume == 100
        assert account.stock_market_value == Decimal("8909.090909")
        assert account.pending_security_value == Decimal("890.909091")
        assert account.corporate_action_receivable == Decimal("200.000000")
        # 10,000 pre-event stock value remains economically continuous after
        # ex price, pending shares and dividend receivable are all included.
        assert account.equity == Decimal("110000.000000")
