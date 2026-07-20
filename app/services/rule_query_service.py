from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from app.common.code_utils import normalize_code
from app.common.exceptions import BusinessRuleError
from app.models.fee_rule import FeeRule
from app.models.instrument import Instrument
from app.models.margin_rule import MarginRule
from app.repositories.fee_rule_repository import FeeRuleRepository
from app.repositories.instrument_repository import InstrumentRepository
from app.repositories.margin_rule_repository import (
    MarginRuleRepository,
)


@dataclass(frozen=True)
class OrderReferenceRules:
    """
    一笔订单所需要的全部交易参考数据。
    """

    instrument: Instrument
    margin_rule: MarginRule
    fee_rule: FeeRule


class RuleQueryService:
    """
    当前交易规则统一查询服务。

    后面的OMS下单校验统一调用这个Service，
    不要分别在订单代码里查询三张表。
    """

    def __init__(
        self,
        instrument_repository: InstrumentRepository,
        margin_repository: MarginRuleRepository,
        fee_repository: FeeRuleRepository,
    ):
        self.instrument_repository = instrument_repository
        self.margin_repository = margin_repository
        self.fee_repository = fee_repository

    def get_order_rules(
        self,
        db: Session,
        exchange_id: str,
        symbol: str,
        trading_day: date,
    ) -> OrderReferenceRules:
        """
        获取下单需要的合约、保证金、手续费规则。

        独立参考数据同步项目已经完成规则交易日、order_book_id、上市和到期
        日期等一致性校验。本交易程序只确认 current 数据存在且合约可交易，
        不重复承担参考数据激活职责。trading_day 参数暂时保留以兼容现有调用。
        """

        _ = trading_day

        exchange_id = normalize_code(exchange_id)
        symbol = normalize_code(symbol)

        instrument = self.instrument_repository.get(
            db=db,
            exchange_id=exchange_id,
            symbol=symbol,
        )

        if instrument is None:
            raise BusinessRuleError(
                "合约不存在"
            )

        if not instrument.is_active:
            raise BusinessRuleError(
                "合约当前不可交易"
            )

        margin_rule = self.margin_repository.get_current(
            db=db,
            exchange_id=exchange_id,
            symbol=symbol,
        )

        if margin_rule is None:
            raise BusinessRuleError(
                "当前保证金规则不存在"
            )

        fee_rule = self.fee_repository.get_current(
            db=db,
            exchange_id=exchange_id,
            symbol=symbol,
        )

        if fee_rule is None:
            raise BusinessRuleError(
                "当前手续费规则不存在"
            )

        return OrderReferenceRules(
            instrument=instrument,
            margin_rule=margin_rule,
            fee_rule=fee_rule,
        )


def get_rule_query_service() -> RuleQueryService:
    return RuleQueryService(
        instrument_repository=InstrumentRepository(),
        margin_repository=MarginRuleRepository(),
        fee_repository=FeeRuleRepository(),
    )
