from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.common.exceptions import BusinessRuleError
from app.enums.instrument_enums import InstrumentType
from app.enums.order_enums import OffsetFlag, OrderDirection
from app.schemas.market_subscription_schema import (
    MarketPreparationStatus,
    OptionMarketPrepareRequest,
)
from app.services.option_market_pre_subscription_service import (
    OptionMarketPreSubscriptionService,
)


EXPIRES_AT = datetime(2026, 8, 4, 1, 3, tzinfo=timezone.utc)


def make_instrument(
    *,
    instrument_id: int,
    order_book_id: str,
    exchange_id: str,
    instrument_type: str,
    underlying_instrument_id: int | None = None,
    is_tradeable: bool = True,
):
    return SimpleNamespace(
        id=instrument_id,
        order_book_id=order_book_id,
        exchange_id=exchange_id,
        symbol=order_book_id,
        instrument_type=instrument_type,
        underlying_instrument_id=underlying_instrument_id,
        is_active=True,
        is_tradeable=is_tradeable,
    )


def make_service(option, underlying, *, snapshots=None, requests=None):
    repository = Mock()
    repository.get.return_value = option
    repository.get_by_id.return_value = underlying
    store = Mock()
    store.request_codes.return_value = EXPIRES_AT
    store.get_account_requests.return_value = requests or {}
    tick_store = Mock()
    tick_store.get_latest_many.return_value = snapshots or {}
    permission = Mock()
    service = OptionMarketPreSubscriptionService(
        instrument_repository=repository,
        pre_subscription_store=store,
        market_tick_store=tick_store,
        permission_service=permission,
    )
    return service, repository, store, tick_store, permission


def make_account():
    return SimpleNamespace(account_id="A001", status="NORMAL")


@pytest.mark.parametrize(
    ("option_type", "underlying_type", "option_code", "underlying_code"),
    [
        (
            InstrumentType.FUTURES_OPTION.value,
            InstrumentType.FUTURES.value,
            "JD2609-C-4000",
            "JD2609",
        ),
        (
            InstrumentType.INDEX_OPTION.value,
            InstrumentType.INDEX.value,
            "IO2609-C-4000",
            "000300.SH",
        ),
    ],
)
def test_prepare_subscribes_option_and_matching_underlying(
    option_type,
    underlying_type,
    option_code,
    underlying_code,
):
    option = make_instrument(
        instrument_id=1,
        order_book_id=option_code,
        exchange_id="CFFEX" if option_type == "INDEX_OPTION" else "DCE",
        instrument_type=option_type,
        underlying_instrument_id=2,
    )
    underlying = make_instrument(
        instrument_id=2,
        order_book_id=underlying_code,
        exchange_id=option.exchange_id,
        instrument_type=underlying_type,
        is_tradeable=underlying_type != InstrumentType.INDEX.value,
    )
    snapshots = {
        (option.exchange_id, option.symbol): {"last_price": "101.5"},
        (underlying.exchange_id, underlying.symbol): {
            "last_price": "4033"
        },
    }
    service, _, store, _, permission = make_service(
        option,
        underlying,
        snapshots=snapshots,
    )
    request = OptionMarketPrepareRequest(
        account_id="A001",
        exchange_id=option.exchange_id,
        symbol=option.symbol,
        direction=OrderDirection.SELL,
        offset_flag=OffsetFlag.OPEN,
    )

    result = service.prepare(
        Mock(),
        account=make_account(),
        request=request,
    )

    assert result.status == MarketPreparationStatus.READY
    assert result.requested_codes == sorted({option_code, underlying_code})
    assert result.ready_codes == sorted({option_code, underlying_code})
    store.request_codes.assert_called_once_with(
        account_id="A001",
        codes=frozenset({option_code, underlying_code}),
    )
    permission.validate.assert_called_once()


def test_prepare_waits_until_both_prices_are_available():
    option = make_instrument(
        instrument_id=1,
        order_book_id="JD2609-C-4000",
        exchange_id="DCE",
        instrument_type=InstrumentType.FUTURES_OPTION.value,
        underlying_instrument_id=2,
    )
    underlying = make_instrument(
        instrument_id=2,
        order_book_id="JD2609",
        exchange_id="DCE",
        instrument_type=InstrumentType.FUTURES.value,
    )
    service, _, _, _, _ = make_service(
        option,
        underlying,
        snapshots={
            ("DCE", "JD2609-C-4000"): {"last_price": "101"},
            ("DCE", "JD2609"): {"last_price": "NaN"},
        },
    )

    result = service.prepare(
        Mock(),
        account=make_account(),
        request=OptionMarketPrepareRequest(
            account_id="A001",
            exchange_id="DCE",
            symbol="JD2609-C-4000",
            direction="SELL",
            offset_flag="OPEN",
        ),
    )

    assert result.status == MarketPreparationStatus.WAITING_MARKET_DATA
    assert result.ready_codes == ["JD2609-C-4000"]
    assert result.latest_prices_available is False


def test_status_is_not_requested_when_account_has_no_active_pair():
    option = make_instrument(
        instrument_id=1,
        order_book_id="JD2609-C-4000",
        exchange_id="DCE",
        instrument_type=InstrumentType.FUTURES_OPTION.value,
        underlying_instrument_id=2,
    )
    underlying = make_instrument(
        instrument_id=2,
        order_book_id="JD2609",
        exchange_id="DCE",
        instrument_type=InstrumentType.FUTURES.value,
    )
    service, _, _, _, _ = make_service(option, underlying)

    result = service.get_status(
        Mock(),
        account_id="A001",
        exchange_id="DCE",
        symbol="JD2609-C-4000",
    )

    assert result.status == MarketPreparationStatus.NOT_REQUESTED
    assert result.expires_at is None


def test_non_option_instrument_is_rejected():
    future = make_instrument(
        instrument_id=1,
        order_book_id="JD2609",
        exchange_id="DCE",
        instrument_type=InstrumentType.FUTURES.value,
    )
    service, _, store, _, _ = make_service(future, None)

    with pytest.raises(BusinessRuleError) as caught:
        service.get_status(
            Mock(),
            account_id="A001",
            exchange_id="DCE",
            symbol="JD2609",
        )

    assert caught.value.error_code == "OPTION_PRE_SUBSCRIPTION_ONLY"
    store.get_account_requests.assert_not_called()


def test_option_and_underlying_type_mismatch_is_rejected():
    option = make_instrument(
        instrument_id=1,
        order_book_id="IO2609-C-4000",
        exchange_id="CFFEX",
        instrument_type=InstrumentType.INDEX_OPTION.value,
        underlying_instrument_id=2,
    )
    wrong_underlying = make_instrument(
        instrument_id=2,
        order_book_id="IF2609",
        exchange_id="CFFEX",
        instrument_type=InstrumentType.FUTURES.value,
    )
    service, _, store, _, _ = make_service(option, wrong_underlying)

    with pytest.raises(BusinessRuleError) as caught:
        service.get_status(
            Mock(),
            account_id="A001",
            exchange_id="CFFEX",
            symbol="IO2609-C-4000",
        )

    assert caught.value.error_code == "OPTION_UNDERLYING_TYPE_MISMATCH"
    store.get_account_requests.assert_not_called()
