import json
from datetime import date
from types import SimpleNamespace

import pytest

from app.scripts import run_daily_settlement
from app.services.daily_settlement_service import DailySettlementError


@pytest.mark.parametrize(
    "value",
    ["2026-8-06", "2026/08/06", "2026-08-06T00:00:00", "2026-02-30"],
)
def test_parse_trading_day_is_strict(value):
    with pytest.raises(Exception):
        run_daily_settlement.parse_trading_day(value)


def test_cli_success_returns_zero_and_json(monkeypatch, capsys):
    class Service:
        def __init__(self, **kwargs):
            assert kwargs["settlement_price_provider"] is not None

        def run(self, trading_day):
            return SimpleNamespace(
                batch_id="B-1",
                trading_day=trading_day,
                status="COMPLETED",
                current_stage="COMPLETED",
                accounts_settled=2,
                already_completed=False,
                cache_status="COMPLETED",
                cache_message=None,
            )

    monkeypatch.setattr(run_daily_settlement, "DailySettlementService", Service)

    code = run_daily_settlement.main(["--trading-day", "2026-08-06"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["status"] == "COMPLETED"
    assert payload["accounts_settled"] == 2


def test_cli_business_failure_is_nonzero_and_has_retry_context(monkeypatch, capsys):
    class Service:
        def __init__(self, **kwargs):
            assert kwargs["settlement_price_provider"] is not None

        def run(self, trading_day):
            raise DailySettlementError(
                "行情缺失",
                stage="PRICES_FROZEN",
                error_code="SETTLEMENT_TICK_MISSING",
                batch_id="B-FAIL",
                account_id="A-1",
                retriable=True,
            )

    monkeypatch.setattr(run_daily_settlement, "DailySettlementService", Service)

    code = run_daily_settlement.main(["--trading-day", "2026-08-06"])

    payload = json.loads(capsys.readouterr().err)
    assert code == 1
    assert payload == {
        "account_id": "A-1",
        "batch_id": "B-FAIL",
        "failed_stage": "PRICES_FROZEN",
        "failure_code": "SETTLEMENT_TICK_MISSING",
        "reason": "行情缺失",
        "retriable": True,
        "retry_command": (
            "python -m app.scripts.run_daily_settlement "
            "--trading-day 2026-08-06"
        ),
        "status": "FAILED",
        "trading_day": date(2026, 8, 6).isoformat(),
    }
