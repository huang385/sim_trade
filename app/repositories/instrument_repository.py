from datetime import date, datetime
from decimal import Decimal
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.instrument import Instrument


class InstrumentRepository:
    """
    合约数据库仓储。
    """

    @staticmethod
    def get(
        db: Session,
        exchange_id: str,
        symbol: str,
    ) -> Instrument | None:
        statement = select(Instrument).where(
            Instrument.exchange_id == exchange_id,
            Instrument.symbol == symbol,
        )

        return db.scalar(statement)

    @staticmethod
    def list_all(
        db: Session,
        *,
        exchange_id: str | None = None,
        only_active: bool | None = None,
    ) -> Sequence[Instrument]:
        statement = select(Instrument)

        if exchange_id is not None:
            statement = statement.where(
                Instrument.exchange_id == exchange_id
            )

        if only_active is not None:
            statement = statement.where(
                Instrument.is_active == only_active
            )

        statement = statement.order_by(
            Instrument.exchange_id,
            Instrument.symbol,
        )

        return db.scalars(statement).all()

    @staticmethod
    def upsert(
        db: Session,
        *,
        order_book_id: str,
        symbol: str,
        exchange_id: str,
        instrument_name: str | None,
        product_id: str | None,
        market_type: str,
        contract_multiplier: Decimal,
        price_tick: Decimal,
        min_volume: int,
        max_volume: int,
        listed_date: date | None,
        expire_date: date | None,
        is_active: bool,
        data_source: str,
        synced_at: datetime,
        updated_at: datetime,
    ) -> None:
        statement = insert(Instrument).values(
            order_book_id=order_book_id,
            symbol=symbol,
            exchange_id=exchange_id,
            instrument_name=instrument_name,
            product_id=product_id,
            market_type=market_type,
            contract_multiplier=contract_multiplier,
            price_tick=price_tick,
            min_volume=min_volume,
            max_volume=max_volume,
            listed_date=listed_date,
            expire_date=expire_date,
            is_active=is_active,
            data_source=data_source,
            synced_at=synced_at,
            updated_at=updated_at,
        )

        statement = statement.on_conflict_do_update(
            constraint="uq_instrument_exchange_symbol",
            set_={
                "order_book_id": statement.excluded.order_book_id,
                "instrument_name": statement.excluded.instrument_name,
                "product_id": statement.excluded.product_id,
                "market_type": statement.excluded.market_type,
                "contract_multiplier": (
                    statement.excluded.contract_multiplier
                ),
                "price_tick": statement.excluded.price_tick,
                "min_volume": statement.excluded.min_volume,
                "max_volume": statement.excluded.max_volume,
                "listed_date": statement.excluded.listed_date,
                "expire_date": statement.excluded.expire_date,
                "is_active": statement.excluded.is_active,
                "data_source": statement.excluded.data_source,
                "synced_at": statement.excluded.synced_at,
                "updated_at": statement.excluded.updated_at,
            },
        )

        db.execute(statement)