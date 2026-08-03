from datetime import date

from sqlalchemy import case, or_, select
from sqlalchemy.orm import Session

from app.models.option_margin_rule import OptionMarginRule


class OptionMarginRuleRepository:
    """期权保证金规则查询；Repository不控制事务。"""

    @staticmethod
    def resolve(
        db: Session,
        *,
        instrument_id: int,
        product_id: str | None,
        exchange_id: str,
        instrument_type: str,
        trading_day: date,
    ) -> OptionMarginRule | None:
        # 合约级优先，其次品种级，最后交易所级。每个范围只选择当前
        # 交易日启用规则；规则版本由上游同步服务通过新增记录维护。
        priority = case(
            (OptionMarginRule.instrument_id == instrument_id, 1),
            (
                (
                    OptionMarginRule.instrument_id.is_(None)
                    & (OptionMarginRule.product_id == product_id)
                ),
                2,
            ),
            else_=3,
        )
        statement = (
            select(OptionMarginRule)
            .where(
                OptionMarginRule.exchange_id == exchange_id,
                OptionMarginRule.instrument_type == instrument_type,
                OptionMarginRule.trading_day == trading_day,
                OptionMarginRule.is_active.is_(True),
                or_(
                    OptionMarginRule.instrument_id == instrument_id,
                    (
                        OptionMarginRule.instrument_id.is_(None)
                        & (OptionMarginRule.product_id == product_id)
                    ),
                    (
                        OptionMarginRule.instrument_id.is_(None)
                        & OptionMarginRule.product_id.is_(None)
                    ),
                ),
            )
            .order_by(priority, OptionMarginRule.id.desc())
            .limit(1)
        )
        return db.scalar(statement)

    @staticmethod
    def add(db: Session, rule: OptionMarginRule) -> None:
        db.add(rule)

