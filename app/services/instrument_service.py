from typing import Sequence

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.common.code_utils import normalize_code
from app.common.exceptions import (
    DataAccessError,
    ResourceNotFoundError,
)
from app.common.time_utils import utc_now
from app.enums.reference_data_enums import ReferenceDataSource
from app.models.instrument import Instrument
from app.repositories.instrument_repository import (
    InstrumentRepository,
)
from app.schemas.instrument_schema import InstrumentCreate


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

        try:
            self.repository.upsert(
                db=db,
                order_book_id=request.order_book_id,
                symbol=request.symbol,
                exchange_id=request.exchange_id,
                instrument_name=request.instrument_name,
                product_id=request.product_id,
                market_type=request.market_type.value,
                contract_multiplier=request.contract_multiplier,
                price_tick=request.price_tick,
                min_volume=request.min_volume,
                max_volume=request.max_volume,
                listed_date=request.listed_date,
                expire_date=request.expire_date,
                is_active=request.is_active,
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


def get_instrument_service() -> InstrumentService:
    return InstrumentService(
        repository=InstrumentRepository(),
    )
