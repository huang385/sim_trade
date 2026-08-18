from datetime import date
from collections.abc import Sequence

from sqlalchemy import case, or_, select
from sqlalchemy.orm import Session

from app.common.exceptions import DataAccessError
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

        return FeeRuleItemRepository.resolve_cash_security_components(
            db,
            instrument_id=instrument_id,
            product_id=product_id,
            exchange_id=exchange_id,
            instrument_type="STOCK",
            direction=direction,
            trading_day=trading_day,
            ambiguous_error_code="STOCK_FEE_COMPONENT_AMBIGUOUS",
        )

    @staticmethod
    def resolve_cash_security_components(
        db: Session,
        *,
        instrument_id: int,
        product_id: str | None,
        exchange_id: str,
        instrument_type: str,
        direction: str,
        trading_day: date,
        ambiguous_error_code: str = "CASH_SECURITY_FEE_COMPONENT_AMBIGUOUS",
    ) -> Sequence[FeeRuleItem]:
        """Resolve immutable fee components for a cash-security order."""

        if instrument_type not in {"STOCK", "CONVERTIBLE_BOND"}:
            raise ValueError("cash-security fee lookup requires a cash instrument")
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
        rows = db.execute(
            select(FeeRuleItem, priority.label("scope_priority"))
            .where(
                FeeRuleItem.exchange_id == exchange_id,
                FeeRuleItem.instrument_type == instrument_type,
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
        selected_priority: dict[str, int] = {}
        for row, scope_priority in rows:
            fee_type = row.fee_type
            priority_value = int(scope_priority)
            current_priority = selected_priority.get(fee_type)
            if current_priority is None:
                selected[fee_type] = row
                selected_priority[fee_type] = priority_value
                continue
            if current_priority == priority_value:
                # 同一费用类型在最高适用范围内不唯一时，不能靠数据库排序猜测。
                raise DataAccessError(
                    "现金证券手续费规则存在同优先级歧义",
                    error_code=ambiguous_error_code,
                )
        return tuple(selected.values())

    @staticmethod
    def add(db: Session, rule: FeeRuleItem) -> None:
        db.add(rule)
