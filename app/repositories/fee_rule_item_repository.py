from datetime import date

from sqlalchemy import case, or_, select
from sqlalchemy.orm import Session

from app.models.fee_rule_item import FeeRuleItem


class FeeRuleItemRepository:
    """方向化手续费规则查询；规则记录只新增，不在此处更新。"""

    @staticmethod
    def resolve(
        db: Session,
        *,
        instrument_id: int,
        product_id: str | None,
        exchange_id: str,
        instrument_type: str,
        direction: str,
        offset_flag: str,
        trading_day: date,
    ) -> FeeRuleItem | None:
        priority = case(
            (FeeRuleItem.instrument_id == instrument_id, 1),
            (
                (
                    FeeRuleItem.instrument_id.is_(None)
                    & (FeeRuleItem.product_id == product_id)
                ),
                2,
            ),
            else_=3,
        )
        statement = (
            select(FeeRuleItem)
            .where(
                FeeRuleItem.exchange_id == exchange_id,
                FeeRuleItem.instrument_type == instrument_type,
                FeeRuleItem.direction == direction,
                FeeRuleItem.offset_flag == offset_flag,
                FeeRuleItem.fee_type == "DERIVATIVE_COMMISSION",
                FeeRuleItem.trading_day == trading_day,
                FeeRuleItem.is_active.is_(True),
                or_(
                    FeeRuleItem.instrument_id == instrument_id,
                    (
                        FeeRuleItem.instrument_id.is_(None)
                        & (FeeRuleItem.product_id == product_id)
                    ),
                    (
                        FeeRuleItem.instrument_id.is_(None)
                        & FeeRuleItem.product_id.is_(None)
                    ),
                ),
            )
            .order_by(priority, FeeRuleItem.id.desc())
            .limit(1)
        )
        return db.scalar(statement)

    @staticmethod
    def resolve_stock_components(
        db: Session,
        *,
        instrument_id: int,
        product_id: str | None,
        exchange_id: str,
        direction: str,
        trading_day: date,
    ) -> Sequence[FeeRuleItem]:
        """一次读取股票当日的所有费用组件，并按类型保证确定性。"""

        priority = case(
            (FeeRuleItem.instrument_id == instrument_id, 1),
            (
                (
                    FeeRuleItem.instrument_id.is_(None)
                    & (FeeRuleItem.product_id == product_id)
                ),
                2,
            ),
            else_=3,
        )
        rows = db.scalars(
            select(FeeRuleItem)
            .where(
                FeeRuleItem.exchange_id == exchange_id,
                FeeRuleItem.instrument_type == "STOCK",
                FeeRuleItem.direction == direction,
                FeeRuleItem.offset_flag.is_(None),
                FeeRuleItem.trading_day == trading_day,
                FeeRuleItem.is_active.is_(True),
                or_(
                    FeeRuleItem.instrument_id == instrument_id,
                    (
                        FeeRuleItem.instrument_id.is_(None)
                        & (FeeRuleItem.product_id == product_id)
                    ),
                    (
                        FeeRuleItem.instrument_id.is_(None)
                        & FeeRuleItem.product_id.is_(None)
                    ),
                ),
            )
            .order_by(FeeRuleItem.fee_type, priority, FeeRuleItem.id.desc())
        ).all()
        selected: dict[str, FeeRuleItem] = {}
        for row in rows:
            selected.setdefault(row.fee_type, row)
        return tuple(selected.values())

    @staticmethod
    def add(db: Session, rule: FeeRuleItem) -> None:
        db.add(rule)
