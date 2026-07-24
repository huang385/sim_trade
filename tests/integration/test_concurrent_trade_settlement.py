from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.database import SessionLocal
from app.models.account import Account
from app.matching.models import MatchResult
from app.models.position import Position
from app.models.trade import Trade
from app.services.trade_settlement_service import TradeSettlementService
from tests.integration.conftest import make_order_service, make_request


pytestmark = pytest.mark.integration


def test_same_account_concurrent_fills_preserve_funds_and_position(
    integration_context,
):
    """账户行锁使不同订单的并发成交按账户串行转换冻结资源。"""

    order_ids = []
    for index in range(2):
        with SessionLocal() as db:
            order = make_order_service(integration_context).create_order(
                db,
                make_request(
                    integration_context,
                    client_order_id=f"CONCURRENT-FILL-{index}",
                    volume=1,
                ),
            )
            order_ids.append(order.order_id)

    def settle(index):
        result = MatchResult(
            matched=True,
            order_id=order_ids[index],
            market_event_id=f"CONCURRENT-TICK-{index}",
            market_stream_message_id=f"{index + 1}-0",
            fill_price=Decimal("3499"),
            fill_volume=1,
            tick_event_time=datetime(2026, 7, 23, 1, tzinfo=timezone.utc),
            tick_sequence_id=index + 1,
            reason=None,
            engine_name="VN",
            engine_version="1.0",
        )
        with SessionLocal() as db:
            return TradeSettlementService().settle(db, result).action

    with ThreadPoolExecutor(max_workers=2) as executor:
        actions = list(executor.map(settle, range(2)))
    assert actions == ["SETTLED", "SETTLED"]

    with SessionLocal() as db:
        account = db.scalar(
            select(Account).where(
                Account.account_id == integration_context.account_id
            )
        )
        position = db.scalar(
            select(Position).where(
                Position.account_id == integration_context.account_id
            )
        )
        trades = db.scalars(
            select(Trade).where(Trade.account_id == integration_context.account_id)
        ).all()
        assert len(trades) == 2
        assert position.total_volume == 2
        assert position.available_volume == 2
        assert position.used_margin == Decimal("8400.000000")
        assert account.available_cash == Decimal("91594.000000")
        assert account.frozen_margin == Decimal("0.000000")
        assert account.used_margin == Decimal("8400.000000")
        assert account.frozen_commission == Decimal("0.000000")
        assert account.used_commission == Decimal("6.000000")
        assert account.cash_balance == Decimal("99994.000000")
