from datetime import date
from typing import Sequence

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.common.code_utils import normalize_code
from app.common.exceptions import DataAccessError, ResourceNotFoundError
from app.common.time_utils import utc_now
from app.enums.reference_data_enums import ReferenceDataSource
from app.models.fee_rule import FeeRule
from app.models.fee_rule_daily import FeeRuleDaily
from app.repositories.fee_rule_repository import FeeRuleRepository
from app.schemas.fee_rule_schema import FeeRuleCreate, FeeRuleDailyCreate


class FeeRuleService:
    """手续费规则业务服务。"""

    def __init__(self, repository: FeeRuleRepository):
        self.repository = repository

    def upsert_current_manual(
        self,
        db: Session,
        request: FeeRuleCreate,
    ) -> FeeRule:
        current_time = utc_now()
        try:
            self.repository.upsert_current(
                db=db,
                order_book_id=request.order_book_id,
                symbol=request.symbol,
                exchange_id=request.exchange_id,
                trading_day=request.trading_day,
                commission_type=request.commission_type.value,
                open_commission=request.open_commission,
                close_commission=request.close_commission,
                close_today_commission=request.close_today_commission,
                discount_rate=request.discount_rate,
                data_source=ReferenceDataSource.MANUAL.value,
                synced_at=current_time,
                updated_at=current_time,
            )
            db.commit()
        except SQLAlchemyError as exc:
            db.rollback()
            raise DataAccessError("保存当前手续费规则失败") from exc

        rule = self.repository.get_current(
            db=db,
            exchange_id=request.exchange_id,
            symbol=request.symbol,
        )
        if rule is None:
            raise DataAccessError("当前手续费规则保存后查询失败")
        return rule

    def get_current(
        self,
        db: Session,
        exchange_id: str,
        symbol: str,
    ) -> FeeRule:
        rule = self.repository.get_current(
            db=db,
            exchange_id=normalize_code(exchange_id),
            symbol=normalize_code(symbol),
        )
        if rule is None:
            raise ResourceNotFoundError("当前手续费规则不存在")
        return rule

    def list_current(
        self,
        db: Session,
        exchange_id: str | None,
    ) -> Sequence[FeeRule]:
        normalized_exchange_id = (
            normalize_code(exchange_id)
            if exchange_id is not None
            else None
        )
        return self.repository.list_current(
            db=db,
            exchange_id=normalized_exchange_id,
        )

    def upsert_daily_manual(
        self,
        db: Session,
        request: FeeRuleDailyCreate,
    ) -> FeeRuleDaily:
        current_time = utc_now()
        try:
            self.repository.upsert_daily(
                db=db,
                order_book_id=request.order_book_id,
                symbol=request.symbol,
                exchange_id=request.exchange_id,
                trading_day=request.trading_day,
                commission_type=request.commission_type.value,
                open_commission=request.open_commission,
                close_commission=request.close_commission,
                close_today_commission=request.close_today_commission,
                discount_rate=request.discount_rate,
                data_source=ReferenceDataSource.MANUAL.value,
                sync_batch_id=request.sync_batch_id,
                synced_at=current_time,
                updated_at=current_time,
            )
            db.commit()
        except SQLAlchemyError as exc:
            db.rollback()
            raise DataAccessError("保存逐交易日手续费规则失败") from exc

        rule = self.repository.get_daily(
            db=db,
            trading_day=request.trading_day,
            exchange_id=request.exchange_id,
            symbol=request.symbol,
        )
        if rule is None:
            raise DataAccessError("逐交易日手续费规则保存后查询失败")
        return rule

    def get_daily(
        self,
        db: Session,
        trading_day: date,
        exchange_id: str,
        symbol: str,
    ) -> FeeRuleDaily:
        rule = self.repository.get_daily(
            db=db,
            trading_day=trading_day,
            exchange_id=normalize_code(exchange_id),
            symbol=normalize_code(symbol),
        )
        if rule is None:
            raise ResourceNotFoundError("指定交易日的手续费规则不存在")
        return rule

    def list_daily(
        self,
        db: Session,
        trading_day: date,
        exchange_id: str | None,
    ) -> Sequence[FeeRuleDaily]:
        normalized_exchange_id = (
            normalize_code(exchange_id)
            if exchange_id is not None
            else None
        )
        return self.repository.list_daily(
            db=db,
            trading_day=trading_day,
            exchange_id=normalized_exchange_id,
        )


def get_fee_rule_service() -> FeeRuleService:
    return FeeRuleService(repository=FeeRuleRepository())
