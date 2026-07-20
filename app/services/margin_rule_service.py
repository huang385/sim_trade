from datetime import date
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
from app.models.margin_rule import MarginRule
from app.models.margin_rule_daily import MarginRuleDaily
from app.repositories.margin_rule_repository import (
    MarginRuleRepository,
)
from app.schemas.margin_rule_schema import (
    MarginRuleCreate,
    MarginRuleDailyCreate,
)


class MarginRuleService:
    """
    保证金规则业务服务。
    """

    def __init__(
        self,
        repository: MarginRuleRepository,
    ):
        self.repository = repository

    def upsert_current_manual(
        self,
        db: Session,
        request: MarginRuleCreate,
    ) -> MarginRule:
        current_time = utc_now()

        try:
            self.repository.upsert_current(
                db=db,
                order_book_id=request.order_book_id,
                symbol=request.symbol,
                exchange_id=request.exchange_id,
                trading_day=request.trading_day,
                long_margin_rate=request.long_margin_rate,
                short_margin_rate=request.short_margin_rate,
                min_margin_rate=request.min_margin_rate,
                data_source=ReferenceDataSource.MANUAL.value,
                synced_at=current_time,
                updated_at=current_time,
            )

            db.commit()

        except SQLAlchemyError as exc:
            db.rollback()

            raise DataAccessError(
                "保存当前保证金规则失败"
            ) from exc

        rule = self.repository.get_current(
            db=db,
            exchange_id=request.exchange_id,
            symbol=request.symbol,
        )

        if rule is None:
            raise DataAccessError(
                "当前保证金规则保存后查询失败"
            )

        return rule

    def get_current(
        self,
        db: Session,
        exchange_id: str,
        symbol: str,
    ) -> MarginRule:
        exchange_id = normalize_code(exchange_id)
        symbol = normalize_code(symbol)

        rule = self.repository.get_current(
            db=db,
            exchange_id=exchange_id,
            symbol=symbol,
        )

        if rule is None:
            raise ResourceNotFoundError(
                "当前保证金规则不存在"
            )

        return rule

    def list_current(
        self,
        db: Session,
        exchange_id: str | None,
    ) -> Sequence[MarginRule]:
        normalized_exchange_id = None

        if exchange_id is not None:
            normalized_exchange_id = normalize_code(
                exchange_id
            )

        return self.repository.list_current(
            db=db,
            exchange_id=normalized_exchange_id,
        )

    def upsert_daily_manual(
        self,
        db: Session,
        request: MarginRuleDailyCreate,
    ) -> MarginRuleDaily:
        current_time = utc_now()

        try:
            self.repository.upsert_daily(
                db=db,
                order_book_id=request.order_book_id,
                symbol=request.symbol,
                exchange_id=request.exchange_id,
                trading_day=request.trading_day,
                long_margin_rate=request.long_margin_rate,
                short_margin_rate=request.short_margin_rate,
                min_margin_rate=request.min_margin_rate,
                data_source=ReferenceDataSource.MANUAL.value,
                sync_batch_id=request.sync_batch_id,
                synced_at=current_time,
                updated_at=current_time,
            )

            db.commit()

        except SQLAlchemyError as exc:
            db.rollback()

            raise DataAccessError(
                "保存逐交易日保证金规则失败"
            ) from exc

        rule = self.repository.get_daily(
            db=db,
            trading_day=request.trading_day,
            exchange_id=request.exchange_id,
            symbol=request.symbol,
        )

        if rule is None:
            raise DataAccessError(
                "逐交易日保证金规则保存后查询失败"
            )

        return rule

    def get_daily(
        self,
        db: Session,
        trading_day: date,
        exchange_id: str,
        symbol: str,
    ) -> MarginRuleDaily:
        exchange_id = normalize_code(exchange_id)
        symbol = normalize_code(symbol)

        rule = self.repository.get_daily(
            db=db,
            trading_day=trading_day,
            exchange_id=exchange_id,
            symbol=symbol,
        )

        if rule is None:
            raise ResourceNotFoundError(
                "指定交易日的保证金规则不存在"
            )

        return rule

    def list_daily(
        self,
        db: Session,
        trading_day: date,
        exchange_id: str | None,
    ) -> Sequence[MarginRuleDaily]:
        normalized_exchange_id = None

        if exchange_id is not None:
            normalized_exchange_id = normalize_code(
                exchange_id
            )

        return self.repository.list_daily(
            db=db,
            trading_day=trading_day,
            exchange_id=normalized_exchange_id,
        )


def get_margin_rule_service() -> MarginRuleService:
    return MarginRuleService(
        repository=MarginRuleRepository(),
    )
