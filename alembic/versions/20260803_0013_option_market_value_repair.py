"""重新校正已升级数据库中的历史期权持仓标记市值。

Revision ID: 20260803_0013
Revises: 20260803_0012
"""

from alembic import op


revision = "20260803_0013"
down_revision = "20260803_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    0012首次在本地执行时，历史乘数修复可能晚于期权市值回填。

    本数据迁移只使用已经校正且受正数约束保护的乘数快照重新计算派生
    市值，因此可重复审计，也能安全覆盖此前因空乘数产生的0值。
    """

    op.execute(
        """
        UPDATE position
        SET option_market_value = ROUND(
            COALESCE(margin_option_price, average_open_price)
            * multiplier_snapshot
            * total_volume,
            6
        )
        WHERE instrument_type IN ('FUTURES_OPTION', 'INDEX_OPTION')
        """
    )


def downgrade() -> None:
    # 这是对派生快照的纠错，旧错误值不可可靠恢复；降级保留正确数据。
    pass
