"""现金证券订单的资金冻结与释放。

该服务刻意只处理成交款和手续费冻结，不读取或修改保证金、风险可用资金等
衍生品口径字段。调用方必须已经在同一数据库事务中锁定账户。
"""

from decimal import Decimal

from app.common.decimal_utils import quantize_money
from app.common.exceptions import (
    BusinessRuleError,
    BusinessValidationError,
    DataAccessError,
    ResourceNotFoundError,
)
from app.enums.account_enums import AccountStatus
from app.models.account import Account


class CashSecurityFundsService:
    @staticmethod
    def validate_account_tradable(account: Account | None) -> None:
        if account is None:
            raise ResourceNotFoundError(
                "账户不存在", error_code="ACCOUNT_NOT_FOUND"
            )
        if account.status != AccountStatus.NORMAL.value:
            raise BusinessRuleError(
                "账户当前不可交易", error_code="ACCOUNT_NOT_TRADABLE"
            )

    @classmethod
    def freeze_buy(
        cls,
        *,
        account: Account | None,
        frozen_cash: Decimal,
        frozen_commission: Decimal,
    ) -> None:
        cls.validate_account_tradable(account)
        cash = quantize_money(frozen_cash)
        commission = quantize_money(frozen_commission)
        if cash < Decimal("0") or commission < Decimal("0"):
            raise BusinessValidationError(
                "现金证券买入冻结金额不能小于 0",
                error_code="INVALID_CASH_SECURITY_FROZEN_RESOURCE",
            )
        required = quantize_money(cash + commission)
        if quantize_money(account.available_cash) < required:
            raise BusinessRuleError(
                "账户可用资金不足", error_code="INSUFFICIENT_AVAILABLE_CASH"
            )
        account.available_cash = quantize_money(account.available_cash - required)
        account.frozen_cash = quantize_money(account.frozen_cash + cash)
        account.frozen_commission = quantize_money(
            account.frozen_commission + commission
        )

    @staticmethod
    def release_buy(
        *,
        account: Account | None,
        frozen_cash: Decimal,
        frozen_commission: Decimal,
    ) -> None:
        if account is None:
            raise ResourceNotFoundError(
                "账户不存在", error_code="ACCOUNT_NOT_FOUND"
            )
        cash = quantize_money(frozen_cash)
        commission = quantize_money(frozen_commission)
        if cash < Decimal("0") or commission < Decimal("0"):
            raise BusinessValidationError(
                "现金证券释放金额不能小于 0",
                error_code="INVALID_CASH_SECURITY_RELEASE_RESOURCE",
            )
        if account.frozen_cash < cash or account.frozen_commission < commission:
            raise DataAccessError(
                "账户冻结资金不足以释放现金证券订单",
                error_code="CASH_SECURITY_CANCEL_FROZEN_RESOURCE_INCONSISTENT",
            )
        account.available_cash = quantize_money(
            account.available_cash + cash + commission
        )
        account.frozen_cash = quantize_money(account.frozen_cash - cash)
        account.frozen_commission = quantize_money(
            account.frozen_commission - commission
        )
