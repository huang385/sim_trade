import argparse
import json
import re
import sys
from datetime import date

from app.services.daily_settlement_service import (
    DailySettlementError,
    DailySettlementService,
)


DAY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_trading_day(value: str) -> date:
    """只接受完整 ISO 日期，拒绝宽松或含时间的输入。"""

    if not DAY_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("交易日必须使用 YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("交易日不是有效日期") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="执行一次手工日终结算")
    parser.add_argument(
        "--trading-day",
        required=True,
        type=parse_trading_day,
        help="需要结算的交易日（YYYY-MM-DD）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    retry_command = (
        "python -m app.scripts.run_daily_settlement "
        f"--trading-day {args.trading_day.isoformat()}"
    )
    try:
        result = DailySettlementService().run(args.trading_day)
    except DailySettlementError as exc:
        print(
            json.dumps(
                {
                    "status": "FAILED",
                    "batch_id": exc.batch_id,
                    "trading_day": args.trading_day.isoformat(),
                    "failed_stage": exc.stage,
                    "account_id": exc.account_id,
                    "failure_code": exc.error_code,
                    "reason": exc.message,
                    "retriable": exc.retriable,
                    "retry_command": retry_command if exc.retriable else None,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "status": result.status,
                "batch_id": result.batch_id,
                "trading_day": result.trading_day.isoformat(),
                "current_stage": result.current_stage,
                "accounts_settled": result.accounts_settled,
                "already_completed": result.already_completed,
                "cache_status": result.cache_status,
                "cache_message": result.cache_message,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

