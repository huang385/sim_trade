"""合约与参考规则模块稳定公共入口。"""

from app.services.fee_rule_service import FeeRuleService, get_fee_rule_service
from app.services.instrument_service import InstrumentService, get_instrument_service
from app.services.margin_rule_service import MarginRuleService, get_margin_rule_service
from app.services.rule_query_service import RuleQueryService, get_rule_query_service

__all__ = [
    "FeeRuleService",
    "InstrumentService",
    "MarginRuleService",
    "RuleQueryService",
    "get_fee_rule_service",
    "get_instrument_service",
    "get_margin_rule_service",
    "get_rule_query_service",
]
