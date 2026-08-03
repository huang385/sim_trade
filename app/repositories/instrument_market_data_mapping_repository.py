from typing import Sequence

from sqlalchemy import and_, select
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
    def list_instruments_with_mapping(
        db: Session,
        *,
        data_source: str,
        order_book_ids: set[str] | frozenset[str] | list[str],
    ) -> Sequence[tuple[Instrument, InstrumentMarketDataMapping | None]]:
        """
        批量读取内部合约及其在指定行情源中的启用映射。

        使用左连接兼容内外代码相同的普通期货，也允许期权在尚未人工维护
        映射时使用FeedHub标准代码生成规则。订阅重建时只执行一次该查询，
        实时Tick回调不会访问PostgreSQL。
        """

        normalized_ids = sorted(set(order_book_ids))
        if not normalized_ids:
            return []
        statement = (
            select(Instrument, InstrumentMarketDataMapping)
            .outerjoin(
                InstrumentMarketDataMapping,
                and_(
                    InstrumentMarketDataMapping.instrument_id == Instrument.id,
                    InstrumentMarketDataMapping.data_source == data_source,
                    InstrumentMarketDataMapping.is_enabled.is_(True),
                ),
            )
            .where(Instrument.order_book_id.in_(normalized_ids))
        )
        return list(db.execute(statement).all())

    @staticmethod
    def add(
        db: Session,
        mapping: InstrumentMarketDataMapping,
    ) -> None:
        db.add(mapping)
