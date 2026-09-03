from datetime import date
from typing import Sequence

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.enums.instrument_enums import CASH_SECURITY_INSTRUMENT_TYPES
from app.models.instrument import Instrument
from app.models.stock_trading_rule import StockTradingRule


class StockTradingRuleRepository:
    """股票交易规则仓储；不控制事务提交或回滚。"""

    @staticmethod
    def get_by_instrument_and_version(
        db: Session,
        *,
        instrument_id: int,
        rule_version: str,
    ) -> StockTradingRule | None:
        return db.scalar(
            select(StockTradingRule).where(
                StockTradingRule.instrument_id == instrument_id,
                StockTradingRule.rule_version == rule_version,
            )
        )

    @staticmethod
    def get_stock_instrument_for_update(
        db: Session,
        *,
        instrument_id: int,
    ) -> Instrument | None:
        """串行化同一股票的规则写入，不扩大到其他股票。"""

        return db.scalar(
            select(Instrument)
            .where(
                Instrument.id == instrument_id,
                Instrument.instrument_type.in_(CASH_SECURITY_INSTRUMENT_TYPES),
            )
            .with_for_update()
        )

    @staticmethod
    def list_history(
        db: Session,
        *,
        instrument_id: int,
    ) -> Sequence[StockTradingRule]:
        return db.scalars(
            select(StockTradingRule)
            .where(StockTradingRule.instrument_id == instrument_id)
            .order_by(StockTradingRule.effective_from.desc(), StockTradingRule.id.desc())
        ).all()

    @staticmethod
    def resolve_for_trading_day(
        db: Session,
        *,
        instrument_id: int,
        trading_day: date,
    ) -> StockTradingRule:
        """解析唯一有效规则；缺失和区间冲突均明确失败。"""

        rows = db.scalars(
            select(StockTradingRule).where(
                StockTradingRule.instrument_id == instrument_id,
                StockTradingRule.effective_from <= trading_day,
                or_(
                    StockTradingRule.effective_to.is_(None),
                    StockTradingRule.effective_to >= trading_day,
                ),
            )
        ).all()
        if not rows:
            raise LookupError(
                f"未找到股票交易规则: instrument_id={instrument_id}, "
                f"trading_day={trading_day.isoformat()}"
            )
        if len(rows) != 1:
            raise LookupError(
                f"股票交易规则存在有效期冲突: instrument_id={instrument_id}, "
                f"trading_day={trading_day.isoformat()}"
            )
        return rows[0]

    @staticmethod
    def create(db: Session, rule: StockTradingRule) -> None:
        """新增规则前拒绝版本重复和会导致歧义的生效区间重叠。"""

        instrument = StockTradingRuleRepository.get_stock_instrument_for_update(
            db,
            instrument_id=rule.instrument_id,
        )
        if instrument is None:
            raise ValueError(
                "现金证券交易规则只能关联 STOCK Instrument、"
                "CONVERTIBLE_BOND Instrument 或 ETF Instrument"
            )
        if StockTradingRuleRepository.get_by_instrument_and_version(
            db,
            instrument_id=rule.instrument_id,
            rule_version=rule.rule_version,
        ) is not None:
            raise ValueError("同一股票的规则版本不能重复")

        interval_end = rule.effective_to or date.max
        overlaps = db.scalar(
            select(StockTradingRule.id).where(
                StockTradingRule.instrument_id == rule.instrument_id,
                StockTradingRule.effective_from <= interval_end,
                or_(
                    StockTradingRule.effective_to.is_(None),
                    StockTradingRule.effective_to >= rule.effective_from,
                ),
            ).limit(1)
        )
        if overlaps is not None:
            raise ValueError("同一股票的规则有效期不能重叠")
        db.add(rule)
