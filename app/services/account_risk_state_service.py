from app.enums.account_enums import AccountRiskState


class AccountRiskStateService:
    """统一账户风险状态优先级以及局部流程的写入边界。"""

    @staticmethod
    def resolve_full_evaluation(
        *,
        valuation_unavailable: bool,
        margin_deficit: bool,
    ) -> str:
        """
        只供账户级完整估值使用，可安全恢复 NORMAL。

        状态优先级固定为：估值不可用 > 保证金不足 > 正常。
        """

        if valuation_unavailable:
            return AccountRiskState.VALUATION_UNAVAILABLE.value
        if margin_deficit:
            return AccountRiskState.MARGIN_DEFICIT.value
        return AccountRiskState.NORMAL.value

    @staticmethod
    def preserve_for_local_update(
        current_state: str,
        *,
        valuation_unavailable: bool = False,
        margin_deficit: bool = False,
    ) -> str:
        """
        局部持仓、订单或成交事务只能提高或保持风险，不能恢复 NORMAL。

        风险来源真正解除后，由完整账户估值锁定并核对全部持仓、明细、
        行情、规则和活动订单，再调用 ``resolve_full_evaluation`` 恢复。
        """

        return AccountRiskStateService.resolve_full_evaluation(
            valuation_unavailable=(
                valuation_unavailable
                or current_state
                == AccountRiskState.VALUATION_UNAVAILABLE.value
            ),
            margin_deficit=(
                margin_deficit
                or current_state == AccountRiskState.MARGIN_DEFICIT.value
            ),
        )
