from fastapi import APIRouter

from app.api.account_api import router as account_router
from app.api.instrument_api import router as instrument_router
from app.api.margin_rule_api import router as margin_rule_router
from app.api.fee_rule_api import router as fee_rule_router
from app.api.order_api import router as order_router
from app.api.trade_api import router as trade_router
from app.api.position_api import router as position_router
from app.api.pnl_api import router as pnl_router
from app.api.auth_api import router as auth_router
from app.api.admin_user_api import router as admin_user_router


api_router = APIRouter()

# 登录和Refresh是公开入口，其余认证信息及业务API各自执行统一依赖。
api_router.include_router(auth_router)
api_router.include_router(admin_user_router)


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

# 账户和持仓实时盈亏只读接口
api_router.include_router(pnl_router)
