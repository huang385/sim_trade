from datetime import date
from types import SimpleNamespace

import pytest

from app.common.exceptions import BusinessRuleError
from app.enums.order_enums import OffsetFlag
from app.services.position_close_allocator import PositionCloseAllocator


TRADING_DAY = date(2026, 7, 24)


def detail(detail_id, day, remaining, frozen=0):
    return SimpleNamespace(
        id=detail_id,
        open_trading_day=day,
        remaining_volume=remaining,
        frozen_volume=frozen,
    )


def test_close_today_only_uses_today_position():
    plans = PositionCloseAllocator.allocate(
        details=[
            detail(1, date(2026, 7, 23), 5),
            detail(2, TRADING_DAY, 4),
        ],
        offset_flag=OffsetFlag.CLOSE_TODAY,
        trading_day=TRADING_DAY,
        volume=3,
    )
    assert [(item.detail.id, item.volume) for item in plans] == [(2, 3)]


def test_close_yesterday_only_uses_yesterday_position():
    plans = PositionCloseAllocator.allocate(
        details=[
            detail(1, date(2026, 7, 23), 2),
            detail(2, TRADING_DAY, 5),
        ],
        offset_flag=OffsetFlag.CLOSE_YESTERDAY,
        trading_day=TRADING_DAY,
        volume=2,
    )
    assert [(item.detail.id, item.volume) for item in plans] == [(1, 2)]


def test_close_uses_yesterday_first_then_today_fifo():
    plans = PositionCloseAllocator.allocate(
        details=[
            detail(3, TRADING_DAY, 5),
            detail(2, date(2026, 7, 23), 2),
            detail(1, date(2026, 7, 22), 1),
        ],
        offset_flag=OffsetFlag.CLOSE,
        trading_day=TRADING_DAY,
        volume=5,
    )
    assert [(item.detail.id, item.volume) for item in plans] == [
        (1, 1),
        (2, 2),
        (3, 2),
    ]


@pytest.mark.parametrize(
    ("offset_flag", "error_code"),
    [
        (OffsetFlag.CLOSE_TODAY, "INSUFFICIENT_TODAY_POSITION"),
        (OffsetFlag.CLOSE_YESTERDAY, "INSUFFICIENT_YESTERDAY_POSITION"),
        (OffsetFlag.CLOSE, "INSUFFICIENT_CLOSE_POSITION"),
    ],
)
def test_insufficient_matching_position_is_rejected(offset_flag, error_code):
    with pytest.raises(BusinessRuleError) as exc_info:
        PositionCloseAllocator.allocate(
            details=[detail(1, TRADING_DAY, 1)],
            offset_flag=offset_flag,
            trading_day=TRADING_DAY,
            volume=2,
        )
    assert exc_info.value.error_code == error_code
