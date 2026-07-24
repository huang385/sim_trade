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


class OrderFreezeService:
    """
    开仓订单资金冻结服务。

    本服务接收已经被 SELECT FOR UPDATE 锁定的账户对象，
    检查账户状态和可用资金后直接修改 ORM 字段。

    只允许修改：
        available_cash
        frozen_margin
        frozen_commission

    此时订单尚未成交，因此不能修改：
        cash_balance
        equity
        used_margin
        used_commission
        realized_pnl

    是否提交或回滚由 OrderService 决定。
    """

    @staticmethod
    def freeze_open_order(
        *,
        account: Account | None,
        frozen_margin: Decimal,
        frozen_commission: Decimal,
    ) -> None:
        """检查账户并冻结预计保证金和预计手续费。"""

        # 账户不存在属于资源不存在，不创建拒绝订单。
        if account is None:
            raise ResourceNotFoundError(
                "账户不存在",
                error_code="ACCOUNT_NOT_FOUND",
            )

        # 只有 NORMAL 账户允许继续下单。
        if account.status != AccountStatus.NORMAL.value:
            raise BusinessRuleError(
                "账户当前不可交易",
                error_code="ACCOUNT_NOT_TRADABLE",
            )

        if frozen_margin < Decimal("0"):
            raise BusinessValidationError(
                "冻结保证金不能小于0",
                error_code="INVALID_FROZEN_MARGIN",
            )

        if frozen_commission < Decimal("0"):
            raise BusinessValidationError(
                "冻结手续费不能小于0",
                error_code="INVALID_FROZEN_COMMISSION",
            )

        # 开仓订单需要一次性覆盖预计保证金和预计手续费。
        required_amount = quantize_money(
            frozen_margin + frozen_commission
        )

        # 资金不足时不能先扣一部分，账户字段必须保持原值。
        if account.available_cash < required_amount:
            raise BusinessRuleError(
                "账户可用资金不足",
                error_code="INSUFFICIENT_AVAILABLE_CASH",
            )

        # 以下三个字段会与订单写入一起提交，任何后续异常都会回滚。
        account.available_cash = quantize_money(
            account.available_cash - required_amount
        )
        account.frozen_margin = quantize_money(
            account.frozen_margin + frozen_margin
        )
        account.frozen_commission = quantize_money(
            account.frozen_commission + frozen_commission
        )

    @staticmethod
    def release_open_order_frozen_resources(
        *,
        account: Account | None,
        frozen_margin: Decimal,
        frozen_commission: Decimal,
    ) -> None:
        """
        释放撤单对应的剩余保证金和手续费。

        撤单降低账户风险，因此不检查账户是否为 NORMAL；DISABLED 和
        LIQUIDATION 账户同样允许释放。调用前账户必须已经由事务行锁保护。
        """

        if account is None:
            raise ResourceNotFoundError(
                "账户不存在",
                error_code="ACCOUNT_NOT_FOUND",
            )

        released_margin = quantize_money(frozen_margin)
        released_commission = quantize_money(frozen_commission)
        if released_margin < Decimal("0"):
            raise BusinessValidationError(
                "释放保证金不能小于0",
                error_code="INVALID_RELEASED_MARGIN",
            )
        if released_commission < Decimal("0"):
            raise BusinessValidationError(
                "释放手续费不能小于0",
                error_code="INVALID_RELEASED_COMMISSION",
            )

        # 两项一致性检查必须在修改任何字段之前完成，避免校验第二项失败时
        # 账户只更新了一半。异常由外层撤单事务统一 rollback。
        if account.frozen_margin < released_margin:
            raise DataAccessError(
                "账户冻结保证金不足以释放撤单资源",
                error_code="CANCEL_FROZEN_RESOURCE_INCONSISTENT",
            )
        if account.frozen_commission < released_commission:
            raise DataAccessError(
                "账户冻结手续费不足以释放撤单资源",
                error_code="CANCEL_FROZEN_RESOURCE_INCONSISTENT",
            )

        released_total = quantize_money(
            released_margin + released_commission
        )
        account.available_cash = quantize_money(
            account.available_cash + released_total
        )
        account.frozen_margin = quantize_money(
            account.frozen_margin - released_margin
        )
        account.frozen_commission = quantize_money(
            account.frozen_commission - released_commission
        )
