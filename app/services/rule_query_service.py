from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from app.common.code_utils import normalize_code
from app.common.exceptions import BusinessRuleError
from app.models.fee_rule import FeeRule
from app.models.instrument import Instrument
from app.models.margin_rule import MarginRule
from app.models.fee_rule_item import FeeRuleItem
from app.models.option_margin_rule import OptionMarginRule
from app.repositories.fee_rule_item_repository import FeeRuleItemRepository
from app.repositories.option_margin_rule_repository import (
    OptionMarginRuleRepository,
)
from app.repositories.fee_rule_repository import FeeRuleRepository
from app.repositories.instrument_repository import InstrumentRepository
from app.repositories.margin_rule_repository import (
    MarginRuleRepository,
)
from app.enums.option_enums import InstrumentType


@dataclass(frozen=True)
class OrderReferenceRules:
    """
    一笔订单所需要的全部交易参考数据。
    """

    instrument: Instrument
    margin_rule: MarginRule | None
    fee_rule: FeeRule | None
    fee_rule_item: FeeRuleItem | None = None
    option_margin_rule: OptionMarginRule | None = None
    underlying_instrument: Instrument | None = None
    underlying_margin_rule: MarginRule | None = None


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
        option_margin_repository: OptionMarginRuleRepository | None = None,
        fee_item_repository: FeeRuleItemRepository | None = None,
    ):
        self.instrument_repository = instrument_repository
        self.margin_repository = margin_repository
        self.fee_repository = fee_repository
        self.option_margin_repository = (
            option_margin_repository or OptionMarginRuleRepository()
        )
        self.fee_item_repository = (
            fee_item_repository or FeeRuleItemRepository()
        )

    def get_order_rules(
        self,
        db: Session,
        exchange_id: str,
        symbol: str,
        trading_day: date,
        direction: str | None = None,
        offset_flag: str | None = None,
    ) -> OrderReferenceRules:
        """
        获取下单需要的合约、保证金、手续费规则。

        独立参考数据同步项目已经完成规则交易日、order_book_id、上市和到期
        日期等一致性校验。本交易程序只确认 current 数据存在且合约可交易，
        不重复承担参考数据激活职责。trading_day 参数暂时保留以兼容现有调用。
        """

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

        if instrument.instrument_type == InstrumentType.INDEX.value:
            raise BusinessRuleError(
                "指数合约不能提交交易订单",
                error_code="INDEX_NOT_TRADEABLE",
            )

        if instrument.instrument_type == InstrumentType.FUTURES.value:
            margin_rule = self.margin_repository.get_current(
                db=db,
                exchange_id=exchange_id,
                symbol=symbol,
            )
            if margin_rule is None:
                raise BusinessRuleError("当前保证金规则不存在")
            fee_rule = self.fee_repository.get_current(
                db=db,
                exchange_id=exchange_id,
                symbol=symbol,
            )
            if fee_rule is None:
                raise BusinessRuleError("当前手续费规则不存在")
            return OrderReferenceRules(
                instrument=instrument,
                margin_rule=margin_rule,
                fee_rule=fee_rule,
            )

        if direction is None or offset_flag is None:
            raise BusinessRuleError(
                "期权规则查询缺少买卖或开平参数",
                error_code="OPTION_RULE_CONTEXT_REQUIRED",
            )
        fee_item = self.fee_item_repository.resolve(
            db=db,
            instrument_id=instrument.id,
            product_id=instrument.product_id,
            exchange_id=exchange_id,
            instrument_type=instrument.instrument_type,
            direction=direction,
            offset_flag=offset_flag,
            trading_day=trading_day,
        )
        if fee_item is None:
            raise BusinessRuleError(
                "当前期权手续费规则不存在",
                error_code="OPTION_FEE_RULE_NOT_FOUND",
            )

        underlying = None
        underlying_margin_rule = None
        option_margin_rule = None
        if instrument.underlying_instrument_id is not None:
            underlying = db.get(
                Instrument,
                instrument.underlying_instrument_id,
            )
        if instrument.instrument_type in {
            InstrumentType.FUTURES_OPTION.value,
            InstrumentType.INDEX_OPTION.value,
        } and underlying is None:
            raise BusinessRuleError(
                "期权标的合约不存在",
                error_code="OPTION_UNDERLYING_NOT_FOUND",
            )
        # 只有卖出开仓需要新增卖方保证金。期权买方与平仓不要求当前
        # 保证金规则存在，避免规则更新影响已有持仓的减仓能力。
        if (
            direction == "SELL"
            and offset_flag == "OPEN"
            and instrument.instrument_type in {
                InstrumentType.FUTURES_OPTION.value,
                InstrumentType.INDEX_OPTION.value,
            }
        ):
            option_margin_rule = self.option_margin_repository.resolve(
                db=db,
                instrument_id=instrument.id,
                product_id=instrument.product_id,
                exchange_id=exchange_id,
                instrument_type=instrument.instrument_type,
                trading_day=trading_day,
            )
            if option_margin_rule is None:
                raise BusinessRuleError(
                    "当前期权保证金规则不存在",
                    error_code="OPTION_MARGIN_RULE_NOT_FOUND",
                )
        if (
            underlying is not None
            and underlying.instrument_type == InstrumentType.FUTURES.value
        ):
            underlying_margin_rule = self.margin_repository.get_current(
                db=db,
                exchange_id=underlying.exchange_id,
                symbol=underlying.symbol,
            )
            if (
                direction == "SELL"
                and offset_flag == "OPEN"
                and underlying_margin_rule is None
            ):
                raise BusinessRuleError(
                    "标的期货保证金规则不存在",
                    error_code="UNDERLYING_MARGIN_RULE_NOT_FOUND",
                )

        return OrderReferenceRules(
            instrument=instrument,
            margin_rule=None,
            fee_rule=None,
            fee_rule_item=fee_item,
            option_margin_rule=option_margin_rule,
            underlying_instrument=underlying,
            underlying_margin_rule=underlying_margin_rule,
        )

    def get_option_fee_rule(
        self,
        db: Session,
        *,
        instrument: Instrument,
        direction: str,
        offset_flag: str,
        trading_day: date,
    ) -> FeeRuleItem:
        """按实际平今/平昨标志解析期权手续费明细。"""

        item = self.fee_item_repository.resolve(
            db=db,
            instrument_id=instrument.id,
            product_id=instrument.product_id,
            exchange_id=instrument.exchange_id,
            instrument_type=instrument.instrument_type,
            direction=direction,
            offset_flag=offset_flag,
            trading_day=trading_day,
        )
        if item is None:
            raise BusinessRuleError(
                "当前期权手续费规则不存在",
                error_code="OPTION_FEE_RULE_NOT_FOUND",
            )
        return item


def get_rule_query_service() -> RuleQueryService:
    return RuleQueryService(
        instrument_repository=InstrumentRepository(),
        margin_repository=MarginRuleRepository(),
        fee_repository=FeeRuleRepository(),
        option_margin_repository=OptionMarginRuleRepository(),
        fee_item_repository=FeeRuleItemRepository(),
    )
