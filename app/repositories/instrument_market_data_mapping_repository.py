from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.instrument import Instrument
from app.models.instrument_market_data_mapping import (
    InstrumentMarketDataMapping,
)


class InstrumentMarketDataMappingRepository:
    """行情代码映射的数据访问层，不控制事务。"""

    @staticmethod
    def get_instrument_by_source_code(
        db: Session,
        *,
        data_source: str,
        market_data_code: str,
    ) -> Instrument | None:
        statement = (
            select(Instrument)
            .join(
                InstrumentMarketDataMapping,
                InstrumentMarketDataMapping.instrument_id == Instrument.id,
            )
            .where(
                InstrumentMarketDataMapping.data_source == data_source,
                InstrumentMarketDataMapping.market_data_code
                == market_data_code,
                InstrumentMarketDataMapping.is_enabled.is_(True),
            )
        )
        return db.scalar(statement)

    @staticmethod
    def add(
        db: Session,
        mapping: InstrumentMarketDataMapping,
    ) -> None:
        db.add(mapping)

