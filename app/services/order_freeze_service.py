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
    开平仓订单资源冻结与撤单释放服务。

    本服务接收已经被 SELECT FOR UPDATE 锁定的账户对象，
    检查账户状态和可用资金后直接修改 ORM 字段。

    开仓冻结允许修改：
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
    def validate_account_tradable(account: Account | None) -> None:
        """在冻结任何资源前确认账户存在且允许交易。"""

        if account is None:
            raise ResourceNotFoundError(
                "账户不存在",
                error_code="ACCOUNT_NOT_FOUND",
            )
        if account.status != AccountStatus.NORMAL.value:
            raise BusinessRuleError(
                "账户当前不可交易",
                error_code="ACCOUNT_NOT_TRADABLE",
            )

    @staticmethod
    def freeze_open_order(
        *,
        account: Account | None,
        frozen_margin: Decimal,
        frozen_commission: Decimal,
    ) -> None:
        """检查账户并冻结预计保证金和预计手续费。"""

        OrderFreezeService.validate_account_tradable(account)

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
    def freeze_option_resources(
        *,
        account: Account | None,
        frozen_cash: Decimal,
        frozen_margin: Decimal,
        frozen_commission: Decimal,
    ) -> None:
        """原子冻结期权权利金、卖方保证金和预计手续费。"""

        OrderFreezeService.validate_account_tradable(account)
        values = (frozen_cash, frozen_margin, frozen_commission)
        if any(value < Decimal("0") for value in values):
            raise BusinessValidationError(
                "期权冻结资源不能小于0",
                error_code="INVALID_OPTION_FROZEN_RESOURCE",
            )
        required = quantize_money(sum(values, Decimal("0")))
        # 风险可用资金比账面可用资金更保守时，必须采用更小者。
        spendable = min(
            quantize_money(account.available_cash),
            quantize_money(
                getattr(account, "risk_available_cash", account.available_cash)
            ),
        )
        if spendable < required:
            raise BusinessRuleError(
                "账户风险可用资金不足",
                error_code="INSUFFICIENT_RISK_AVAILABLE_CASH",
            )
        account.available_cash = quantize_money(
            account.available_cash - required
        )
        account.risk_available_cash = quantize_money(
            getattr(account, "risk_available_cash", account.available_cash)
            - required
        )
        account.frozen_cash = quantize_money(
            account.frozen_cash + frozen_cash
        )
        account.frozen_margin = quantize_money(
            account.frozen_margin + frozen_margin
        )
        account.frozen_commission = quantize_money(
            account.frozen_commission + frozen_commission
        )

    @staticmethod
    def freeze_close_order_commission(
        *,
        account: Account | None,
        frozen_commission: Decimal,
    ) -> None:
        """平仓下单只冻结预计手续费，不新增占用保证金。"""

        OrderFreezeService.freeze_open_order(
            account=account,
            frozen_margin=Decimal("0"),
            frozen_commission=frozen_commission,
        )

    @staticmethod
    def release_close_order_commission(
        *,
        account: Account | None,
        frozen_commission: Decimal,
        frozen_cash: Decimal = Decimal("0"),
    ) -> None:
        """平仓撤单只把剩余冻结手续费退回可用资金。"""

        OrderFreezeService.release_open_order_frozen_resources(
            account=account,
            frozen_margin=Decimal("0"),
            frozen_cash=frozen_cash,
            frozen_commission=frozen_commission,
        )

    @staticmethod
    def release_open_order_frozen_resources(
        *,
        account: Account | None,
        frozen_margin: Decimal,
        frozen_commission: Decimal,
        frozen_cash: Decimal = Decimal("0"),
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
        released_cash = quantize_money(frozen_cash)
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
        if released_cash < Decimal("0"):
            raise BusinessValidationError(
                "释放冻结权利金不能小于0",
                error_code="INVALID_RELEASED_CASH",
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
        account_frozen_cash = Decimal(
            getattr(account, "frozen_cash", Decimal("0"))
        )
        if account_frozen_cash < released_cash:
            raise DataAccessError(
                "账户冻结权利金不足以释放撤单资源",
                error_code="CANCEL_FROZEN_RESOURCE_INCONSISTENT",
            )

        released_total = quantize_money(
            released_margin + released_commission + released_cash
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
        # 历史期货对象没有 frozen_cash 时，释放金额必然为0。只有期权
        # 订单真正使用权利金冻结时才要求并修改该字段。
        if hasattr(account, "frozen_cash") or released_cash != Decimal("0"):
            account.frozen_cash = quantize_money(
                account_frozen_cash - released_cash
            )
        if hasattr(account, "risk_available_cash"):
            account.risk_available_cash = quantize_money(
                account.risk_available_cash + released_total
            )
