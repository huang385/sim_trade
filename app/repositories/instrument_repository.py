from datetime import date, datetime
from decimal import Decimal
from typing import Sequence

from sqlalchemy import case, func, or_, select
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
    def get_by_order_book_id(
        db: Session,
        order_book_id: str,
    ) -> Instrument | None:
        """按行情和参考数据统一使用的标准合约编号查询合约。"""

        statement = select(Instrument).where(
            Instrument.order_book_id == order_book_id
        )
        return db.scalar(statement)

    @staticmethod
    def get_by_id(db: Session, instrument_id: int) -> Instrument | None:
        """按数据库主键读取合约，供期权标的关系解析使用。"""

        return db.get(Instrument, instrument_id)

    @staticmethod
    def list_by_order_book_ids(
        db: Session,
        order_book_ids: set[str] | frozenset[str] | list[str],
    ) -> Sequence[Instrument]:
        """一次SQL批量读取订阅集合对应的合约，避免逐合约往返数据库。"""

        normalized_ids = sorted(set(order_book_ids))
        if not normalized_ids:
            return []
        statement = select(Instrument).where(
            Instrument.order_book_id.in_(normalized_ids)
        )
        return db.scalars(statement).all()

    @staticmethod
    def list_by_ids(
        db: Session,
        instrument_ids: set[int] | list[int],
    ) -> Sequence[Instrument]:
        """一次SQL读取一组Instrument主键，供期权标的批量估值。"""

        ids = sorted(set(instrument_ids))
        if not ids:
            return []
        return db.scalars(
            select(Instrument).where(Instrument.id.in_(ids))
        ).all()

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
    def list_tradeable_futures(db: Session) -> Sequence[Instrument]:
        """返回普通交易终端可选择的有效期货合约。"""

        statement = (
            select(Instrument)
            .where(
                Instrument.instrument_type == "FUTURES",
                Instrument.is_active.is_(True),
                Instrument.is_tradeable.is_(True),
            )
            .order_by(Instrument.exchange_id, Instrument.symbol)
        )
        return db.scalars(statement).all()

    @staticmethod
    def search_tradeable_derivatives(
        db: Session,
        *,
        query: str,
        limit: int,
    ) -> Sequence[Instrument]:
        """按相关度搜索可交易期货、商品期权和股指期权。"""

        escaped = (
            query.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        upper_query = escaped.upper()
        contains = f"%{upper_query}%"
        prefix = f"{upper_query}%"

        order_book_id = func.upper(Instrument.order_book_id)
        symbol = func.upper(Instrument.symbol)
        product_id = func.upper(func.coalesce(Instrument.product_id, ""))
        instrument_name = func.upper(
            func.coalesce(Instrument.instrument_name, "")
        )

        relevance = case(
            (order_book_id == upper_query, 0),
            (symbol == upper_query, 0),
            (order_book_id.like(prefix, escape="\\"), 1),
            (symbol.like(prefix, escape="\\"), 1),
            (product_id == upper_query, 2),
            (product_id.like(prefix, escape="\\"), 3),
            (instrument_name.like(prefix, escape="\\"), 4),
            else_=5,
        )
        type_order = case(
            (Instrument.instrument_type == "FUTURES", 0),
            (Instrument.instrument_type == "FUTURES_OPTION", 1),
            else_=2,
        )

        statement = (
            select(Instrument)
            .where(
                Instrument.instrument_type.in_(
                    ("FUTURES", "FUTURES_OPTION", "INDEX_OPTION")
                ),
                Instrument.is_active.is_(True),
                Instrument.is_tradeable.is_(True),
                or_(
                    order_book_id.like(contains, escape="\\"),
                    symbol.like(contains, escape="\\"),
                    product_id.like(contains, escape="\\"),
                    instrument_name.like(contains, escape="\\"),
                ),
            )
            .order_by(
                relevance,
                type_order,
                case((Instrument.expire_date.is_(None), 1), else_=0),
                Instrument.expire_date,
                Instrument.strike_price,
                Instrument.exchange_id,
                Instrument.symbol,
            )
            .limit(limit)
        )
        return db.scalars(statement).all()

    @staticmethod
    def search_tradeable_stocks(
        db: Session,
        *,
        query: str,
        limit: int,
    ) -> Sequence[Instrument]:
        """按代码或名称搜索普通用户可下单的股票和可转债。"""

        escaped = (
            query.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        upper_query = escaped.upper()
        contains = f"%{upper_query}%"
        prefix = f"{upper_query}%"
        order_book_id = func.upper(Instrument.order_book_id)
        symbol = func.upper(Instrument.symbol)
        instrument_name = func.upper(func.coalesce(Instrument.instrument_name, ""))
        relevance = case(
            (order_book_id == upper_query, 0),
            (symbol == upper_query, 0),
            (order_book_id.like(prefix, escape="\\"), 1),
            (symbol.like(prefix, escape="\\"), 1),
            (instrument_name.like(prefix, escape="\\"), 2),
            else_=3,
        )
        statement = (
            select(Instrument)
            .where(
                Instrument.instrument_type.in_(("STOCK", "CONVERTIBLE_BOND")),
                Instrument.is_active.is_(True),
                Instrument.is_tradeable.is_(True),
                or_(
                    order_book_id.like(contains, escape="\\"),
                    symbol.like(contains, escape="\\"),
                    instrument_name.like(contains, escape="\\"),
                ),
            )
            .order_by(relevance, Instrument.exchange_id, Instrument.symbol)
            .limit(limit)
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
        instrument_type: str,
        underlying_instrument_id: int | None,
        option_type: str | None,
        strike_price: Decimal | None,
        exercise_style: str | None,
        settlement_type: str | None,
        contract_multiplier: Decimal,
        price_tick: Decimal,
        min_volume: int,
        max_volume: int,
        listed_date: date | None,
        expire_date: date | None,
        last_trading_date: date | None,
        is_active: bool,
        is_tradeable: bool,
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
            instrument_type=instrument_type,
            underlying_instrument_id=underlying_instrument_id,
            option_type=option_type,
            strike_price=strike_price,
            exercise_style=exercise_style,
            settlement_type=settlement_type,
            contract_multiplier=contract_multiplier,
            price_tick=price_tick,
            min_volume=min_volume,
            max_volume=max_volume,
            listed_date=listed_date,
            expire_date=expire_date,
            last_trading_date=last_trading_date,
            is_active=is_active,
            is_tradeable=is_tradeable,
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
                "instrument_type": statement.excluded.instrument_type,
                "underlying_instrument_id": (
                    statement.excluded.underlying_instrument_id
                ),
                "option_type": statement.excluded.option_type,
                "strike_price": statement.excluded.strike_price,
                "exercise_style": statement.excluded.exercise_style,
                "settlement_type": statement.excluded.settlement_type,
                "contract_multiplier": (
                    statement.excluded.contract_multiplier
                ),
                "price_tick": statement.excluded.price_tick,
                "min_volume": statement.excluded.min_volume,
                "max_volume": statement.excluded.max_volume,
                "listed_date": statement.excluded.listed_date,
                "expire_date": statement.excluded.expire_date,
                "last_trading_date": statement.excluded.last_trading_date,
                "is_active": statement.excluded.is_active,
                "is_tradeable": statement.excluded.is_tradeable,
                "data_source": statement.excluded.data_source,
                "synced_at": statement.excluded.synced_at,
                "updated_at": statement.excluded.updated_at,
            },
        )

        db.execute(statement)
