from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.cash_security_order_fee_accumulator import CashSecurityOrderFeeAccumulator
from app.models.cash_security_trade_fee_component import CashSecurityTradeFeeComponent


class CashSecurityFeeRepository:
    @staticmethod
    def get_accumulator_for_update(
        db: Session, *, order_id: str, fee_type: str
    ) -> CashSecurityOrderFeeAccumulator | None:
        return db.scalar(
            select(CashSecurityOrderFeeAccumulator)
            .where(
                CashSecurityOrderFeeAccumulator.order_id == order_id,
                CashSecurityOrderFeeAccumulator.fee_type == fee_type,
            )
            .with_for_update()
        )

    @staticmethod
    def add_accumulator(db: Session, accumulator: CashSecurityOrderFeeAccumulator) -> None:
        db.add(accumulator)

    @staticmethod
    def add_trade_components(
        db: Session, components: Sequence[CashSecurityTradeFeeComponent]
    ) -> None:
        db.add_all(components)
