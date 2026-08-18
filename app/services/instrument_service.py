from typing import Sequence

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.common.code_utils import normalize_code
from app.common.exceptions import (
    BusinessValidationError,
    DataAccessError,
    ResourceNotFoundError,
)
from app.common.time_utils import utc_now
from app.enums.reference_data_enums import ReferenceDataSource
from app.enums.instrument_enums import InstrumentType
from app.models.instrument import Instrument
from app.repositories.instrument_repository import (
    InstrumentRepository,
)
from app.schemas.instrument_schema import InstrumentCatalogItem, InstrumentCreate


class InstrumentService:
    """
    合约业务服务。
    """

    def __init__(
        self,
        repository: InstrumentRepository,
    ):
        self.repository = repository

    def upsert_manual(
        self,
        db: Session,
        request: InstrumentCreate,
    ) -> Instrument:
        """
        人工创建或更新合约。

        通过HTTP接口写入的数据统一标记为MANUAL。
        """

        current_time = utc_now()

        underlying = None
        if request.underlying_instrument_id is not None:
            underlying = db.get(Instrument, request.underlying_instrument_id)
            if underlying is None:
                raise BusinessValidationError(
                    "期权标的合约不存在",
                    error_code="OPTION_UNDERLYING_NOT_FOUND",
                )
        if request.instrument_type in {
            InstrumentType.FUTURES_OPTION,
            InstrumentType.INDEX_OPTION,
        }:
            if (
                underlying is None
                or request.option_type is None
                or request.strike_price is None
                or request.expire_date is None
            ):
                raise BusinessValidationError(
                    "期权合约缺少标的、类型、行权价或到期日",
                    error_code="OPTION_INSTRUMENT_INCOMPLETE",
                )
            expected_underlying_type = (
                InstrumentType.FUTURES.value
                if request.instrument_type
                == InstrumentType.FUTURES_OPTION
                else InstrumentType.INDEX.value
            )
            if underlying.instrument_type != expected_underlying_type:
                raise BusinessValidationError(
                    "期权标的合约类型不匹配",
                    error_code="OPTION_UNDERLYING_TYPE_MISMATCH",
                )
        elif request.underlying_instrument_id is not None:
            raise BusinessValidationError(
                "非期权合约不能设置标的合约",
                error_code="UNEXPECTED_UNDERLYING_INSTRUMENT",
            )
        if request.instrument_type in {
            InstrumentType.STOCK,
            InstrumentType.CONVERTIBLE_BOND,
        }:
            if request.market_type.value != "STOCK":
                raise BusinessValidationError(
                    "股票 Instrument 的 market_type 必须为 STOCK",
                    error_code="STOCK_MARKET_TYPE_MISMATCH",
                )
            if request.contract_multiplier != 1:
                raise BusinessValidationError(
                    "股票 Instrument 的 contract_multiplier 必须为 1",
                    error_code="STOCK_MULTIPLIER_INVALID",
                )
        if request.instrument_type == InstrumentType.INDEX:
            request.is_tradeable = False

        try:
            self.repository.upsert(
                db=db,
                order_book_id=request.order_book_id,
                symbol=request.symbol,
                exchange_id=request.exchange_id,
                instrument_name=request.instrument_name,
                product_id=request.product_id,
                market_type=request.market_type.value,
                instrument_type=request.instrument_type.value,
                underlying_instrument_id=request.underlying_instrument_id,
                option_type=(
                    request.option_type.value
                    if request.option_type is not None
                    else None
                ),
                strike_price=request.strike_price,
                exercise_style=(
                    request.exercise_style.value
                    if request.exercise_style is not None
                    else None
                ),
                settlement_type=(
                    request.settlement_type.value
                    if request.settlement_type is not None
                    else None
                ),
                contract_multiplier=request.contract_multiplier,
                price_tick=request.price_tick,
                min_volume=request.min_volume,
                max_volume=request.max_volume,
                listed_date=request.listed_date,
                expire_date=request.expire_date,
                last_trading_date=request.last_trading_date,
                is_active=request.is_active,
                is_tradeable=request.is_tradeable,
                data_source=ReferenceDataSource.MANUAL.value,
                synced_at=current_time,
                updated_at=current_time,
            )

            db.commit()

        except SQLAlchemyError as exc:
            db.rollback()

            raise DataAccessError(
                "保存合约信息失败"
            ) from exc

        instrument = self.repository.get(
            db=db,
            exchange_id=request.exchange_id,
            symbol=request.symbol,
        )

        if instrument is None:
            raise DataAccessError(
                "合约已经保存，但重新查询失败"
            )

        return instrument

    def get_instrument(
        self,
        db: Session,
        exchange_id: str,
        symbol: str,
    ) -> Instrument:
        exchange_id = normalize_code(exchange_id)
        symbol = normalize_code(symbol)

        instrument = self.repository.get(
            db=db,
            exchange_id=exchange_id,
            symbol=symbol,
        )

        if instrument is None:
            raise ResourceNotFoundError(
                "合约不存在"
            )

        return instrument

    def list_instruments(
        self,
        db: Session,
        exchange_id: str | None,
        only_active: bool | None,
    ) -> Sequence[Instrument]:
        normalized_exchange_id = None

        if exchange_id is not None:
            normalized_exchange_id = normalize_code(
                exchange_id
            )

        return self.repository.list_all(
            db=db,
            exchange_id=normalized_exchange_id,
            only_active=only_active,
        )

    def list_tradeable_futures(self, db: Session) -> Sequence[Instrument]:
        """返回桌面交易端允许浏览和订阅的有效期货合约。"""

        return self.repository.list_tradeable_futures(db)

    def search_tradeable_derivatives(
        self,
        db: Session,
        *,
        query: str,
        limit: int,
    ) -> Sequence[InstrumentCatalogItem]:
        """搜索期货和期权，并批量解析期权标的合约代码。"""

        normalized_query = query.strip()
        if not normalized_query:
            raise BusinessValidationError(
                "搜索关键词不能为空",
                error_code="INSTRUMENT_SEARCH_QUERY_EMPTY",
            )

        instruments = self.repository.search_tradeable_derivatives(
            db,
            query=normalized_query,
            limit=limit,
        )
        underlying_ids = {
            item.underlying_instrument_id
            for item in instruments
            if item.underlying_instrument_id is not None
        }
        underlying_codes = {
            item.id: item.order_book_id
            for item in self.repository.list_by_ids(db, underlying_ids)
        }

        return [
            InstrumentCatalogItem(
                order_book_id=item.order_book_id,
                symbol=item.symbol,
                exchange_id=item.exchange_id,
                instrument_name=item.instrument_name,
                product_id=item.product_id,
                instrument_type=item.instrument_type,
                underlying_order_book_id=underlying_codes.get(
                    item.underlying_instrument_id
                ),
                option_type=item.option_type,
                strike_price=item.strike_price,
                expire_date=item.expire_date,
                contract_multiplier=item.contract_multiplier,
                price_tick=item.price_tick,
            )
            for item in instruments
        ]


def get_instrument_service() -> InstrumentService:
    return InstrumentService(
        repository=InstrumentRepository(),
    )
