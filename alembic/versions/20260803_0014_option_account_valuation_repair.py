"""修复期权账户聚合估值并持久化活动订单风险来源。

Revision ID: 20260803_0014
Revises: 20260803_0013
"""

from decimal import Decimal

from alembic import op
import sqlalchemy as sa

from app.core.config import settings


revision = "20260803_0014"
down_revision = "20260803_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 活动期权卖出开仓订单的估值异常和保证金缺口必须保存在PG事实中，
    # 后续账户完整估值才能可靠重建风险状态。
    op.add_column(
        "orders",
        sa.Column(
            "margin_risk_state",
            sa.String(32),
            nullable=False,
            server_default="NORMAL",
        ),
    )
    op.create_check_constraint(
        "ck_order_margin_risk_state_valid",
        "orders",
        "margin_risk_state IN "
        "('NORMAL', 'MARGIN_DEFICIT', 'VALUATION_UNAVAILABLE')",
    )

    ratio = Decimal(settings.option_collateral_ratio)
    if not ratio.is_finite() or ratio < 0:
        raise ValueError("OPTION_COLLATERAL_RATIO必须是非负Decimal")
    ratio_sql = format(ratio, "f")

    # 只修复曾经拥有期权持仓的账户，纯期货账户完全不受影响。聚合值只取
    # total_volume>0的活动持仓；空头市值保存非负绝对值。
    op.execute(
        f"""
        WITH option_accounts AS (
            SELECT DISTINCT account_id
            FROM position
            WHERE instrument_type IN ('FUTURES_OPTION', 'INDEX_OPTION')
        ),
        aggregates AS (
            SELECT
                oa.account_id,
                COALESCE(SUM(
                    CASE WHEN p.total_volume > 0 AND p.direction = 'LONG'
                         THEN p.option_market_value ELSE 0 END
                ), 0)::numeric AS long_value,
                COALESCE(SUM(
                    CASE WHEN p.total_volume > 0 AND p.direction = 'SHORT'
                         THEN p.option_market_value ELSE 0 END
                ), 0)::numeric AS short_value,
                COALESCE(SUM(
                    CASE WHEN p.total_volume > 0 AND p.direction = 'SHORT'
                         THEN p.realtime_required_margin ELSE 0 END
                ), 0)::numeric AS realtime_margin
            FROM option_accounts AS oa
            LEFT JOIN position AS p
              ON p.account_id = oa.account_id
             AND p.instrument_type IN ('FUTURES_OPTION', 'INDEX_OPTION')
            GROUP BY oa.account_id
        ),
        invalid_accounts AS (
            SELECT DISTINCT p.account_id
            FROM position AS p
            WHERE p.instrument_type IN ('FUTURES_OPTION', 'INDEX_OPTION')
              AND p.total_volume > 0
              AND (
                    p.multiplier_snapshot IS NULL
                 OR p.multiplier_snapshot <= 0
                 OR p.option_market_value < 0
                 OR p.realtime_required_margin < 0
                 OR COALESCE((
                        SELECT SUM(pd.remaining_volume)
                        FROM position_detail AS pd
                        WHERE pd.position_id = p.position_id
                          AND pd.remaining_volume > 0
                    ), 0) <> p.total_volume
                 OR EXISTS (
                        SELECT 1
                        FROM position_detail AS pd
                        WHERE pd.position_id = p.position_id
                          AND pd.remaining_volume > 0
                          AND (
                                pd.multiplier_snapshot IS NULL
                             OR pd.multiplier_snapshot <= 0
                             OR pd.multiplier_snapshot
                                <> p.multiplier_snapshot
                          )
                    )
              )
        ),
        values_to_apply AS (
            SELECT
                a.account_id,
                ROUND(g.long_value, 6) AS long_value,
                ROUND(g.short_value, 6) AS short_value,
                ROUND(g.long_value - g.short_value, 6) AS net_value,
                ROUND(g.realtime_margin, 6) AS realtime_margin,
                ROUND(
                    a.cash_balance + a.unrealized_pnl
                    + g.long_value - g.short_value,
                    6
                ) AS new_equity,
                ROUND(
                    a.cash_balance + a.unrealized_pnl
                    + g.long_value * {ratio_sql}
                    - g.short_value - a.used_margin
                    - a.frozen_margin - a.frozen_cash
                    - a.frozen_commission,
                    6
                ) AS new_available,
                ROUND(
                    a.cash_balance + a.unrealized_pnl
                    + g.long_value * {ratio_sql}
                    - g.short_value
                    - (
                        GREATEST(a.used_margin - a.option_used_margin, 0)
                        + GREATEST(
                            a.option_used_margin,
                            g.realtime_margin
                        )
                    )
                    - a.frozen_margin - a.frozen_cash
                    - a.frozen_commission,
                    6
                ) AS new_risk_available,
                (i.account_id IS NOT NULL) AS facts_invalid
            FROM account AS a
            JOIN aggregates AS g ON g.account_id = a.account_id
            LEFT JOIN invalid_accounts AS i
              ON i.account_id = a.account_id
        )
        UPDATE account AS a
        SET long_option_market_value = v.long_value,
            short_option_market_value = v.short_value,
            net_option_market_value = v.net_value,
            option_realtime_required_margin = v.realtime_margin,
            equity = v.new_equity,
            available_cash = v.new_available,
            risk_available_cash = v.new_risk_available,
            risk_state = CASE
                WHEN v.facts_invalid THEN 'VALUATION_UNAVAILABLE'
                WHEN v.new_risk_available < 0 THEN 'MARGIN_DEFICIT'
                ELSE 'NORMAL'
            END
        FROM values_to_apply AS v
        WHERE v.account_id = a.account_id
        """
    )


def downgrade() -> None:
    # 账户聚合值是由持仓事实纠错后的结果，不恢复不可审计的旧错误金额。
    op.drop_constraint(
        "ck_order_margin_risk_state_valid",
        "orders",
        type_="check",
    )
    op.drop_column("orders", "margin_risk_state")
