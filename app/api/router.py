from fastapi import APIRouter

from app.api.account_api import router as account_router
from app.api.instrument_api import router as instrument_router
from app.api.margin_rule_api import router as margin_rule_router
from app.api.fee_rule_api import router as fee_rule_router
from app.api.order_api import router as order_router
from app.api.trade_api import router as trade_router
from app.api.position_api import router as position_router


api_router = APIRouter()


# 账户接口
api_router.include_router(account_router)

# 合约管理接口
api_router.include_router(instrument_router)

# 保证金规则管理接口
api_router.include_router(margin_rule_router)

# 手续费规则管理接口
api_router.include_router(fee_rule_router)

# 订单接收和查询接口
api_router.include_router(order_router)

# 成交与持仓只读查询接口
api_router.include_router(trade_router)
api_router.include_router(position_router)
