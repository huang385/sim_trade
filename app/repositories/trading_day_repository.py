from datetime import date
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session


class TradingDayRepository:
    """交易日历和品种交易时段的只读仓储。"""

    @staticmethod
    def list_candidate_schedules(
        db: Session,
        *,
        exchange_id: str,
        product_code: str,
        instrument_type: str,
        start_day: date,
        end_day: date,
    ) -> list[dict[str, Any]]:
        rows = db.execute(
            text(
                "SELECT schedule.trading_day, schedule.exchange_id, "
                "schedule.product_code, schedule.instrument_type, "
                "schedule.sessions, schedule.status AS schedule_status, "
                "calendar.is_open AS calendar_is_open, "
                "calendar.status AS calendar_status "
                "FROM product_trading_schedule AS schedule "
                "JOIN trading_calendar AS calendar "
                "ON calendar.exchange_id = schedule.exchange_id "
                "AND calendar.trading_day = schedule.trading_day "
                "WHERE schedule.exchange_id = :exchange_id "
                "AND schedule.product_code = :product_code "
                "AND schedule.instrument_type = :instrument_type "
                "AND schedule.trading_day BETWEEN :start_day AND :end_day "
                "ORDER BY schedule.trading_day"
            ),
            {
                "exchange_id": exchange_id,
                "product_code": product_code,
                "instrument_type": instrument_type,
                "start_day": start_day,
                "end_day": end_day,
            },
        ).mappings()
        return [dict(row) for row in rows]

    @staticmethod
    def list_account_candidate_schedules(
        db: Session,
        *,
        instrument_types: tuple[str, ...],
        start_day: date,
        end_day: date,
    ) -> list[dict[str, Any]]:
        """读取开户时用于确定账户交易日的候选交易时段。"""

        statement = text(
            "SELECT schedule.trading_day, schedule.exchange_id, "
            "schedule.product_code, schedule.instrument_type, "
            "schedule.sessions, schedule.status AS schedule_status, "
            "calendar.is_open AS calendar_is_open, "
            "calendar.status AS calendar_status "
            "FROM product_trading_schedule AS schedule "
            "JOIN trading_calendar AS calendar "
            "ON calendar.exchange_id = schedule.exchange_id "
            "AND calendar.trading_day = schedule.trading_day "
            "WHERE schedule.instrument_type IN :instrument_types "
            "AND schedule.trading_day BETWEEN :start_day AND :end_day "
            "ORDER BY schedule.trading_day, schedule.exchange_id, "
            "schedule.product_code"
        ).bindparams(bindparam("instrument_types", expanding=True))
        rows = db.execute(
            statement,
            {
                "instrument_types": instrument_types,
                "start_day": start_day,
                "end_day": end_day,
            },
        ).mappings()
        return [dict(row) for row in rows]

    @staticmethod
    def list_cash_security_candidate_schedules(
        db: Session,
        *,
        exchange_id: str,
        instrument_type: str,
        start_day: date,
        end_day: date,
    ) -> list[dict[str, Any]]:
        """按证券市场和合约类型读取时段，不将单只证券归入期货品种。"""

        rows = db.execute(
            text(
                "SELECT schedule.trading_day, schedule.exchange_id, "
                "schedule.product_code, schedule.instrument_type, "
                "schedule.sessions, schedule.status AS schedule_status, "
                "calendar.is_open AS calendar_is_open, "
                "calendar.status AS calendar_status "
                "FROM product_trading_schedule AS schedule "
                "JOIN trading_calendar AS calendar "
                "ON calendar.exchange_id = schedule.exchange_id "
                "AND calendar.trading_day = schedule.trading_day "
                "WHERE schedule.exchange_id = :exchange_id "
                "AND schedule.instrument_type = :instrument_type "
                "AND schedule.trading_day BETWEEN :start_day AND :end_day "
                "ORDER BY schedule.trading_day, schedule.product_code"
            ),
            {
                "exchange_id": exchange_id,
                "instrument_type": instrument_type,
                "start_day": start_day,
                "end_day": end_day,
            },
        ).mappings()
        return [dict(row) for row in rows]
