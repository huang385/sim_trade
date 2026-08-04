"""把行情代码映射从旧FeedHub标识迁移到YMM Live Data。

Revision ID: 20260804_0015
Revises: 20260803_0014
"""

from alembic import op


revision = "20260804_0015"
down_revision = "20260803_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """保留已有期权及特殊合约代码映射，只更新其行情源身份。"""

    op.execute(
        """
        UPDATE instrument_market_data_mapping
        SET data_source = 'YMM_LIVE_DATA',
            updated_at = CURRENT_TIMESTAMP
        WHERE data_source = 'YML_FEEDHUB'
        """
    )


def downgrade() -> None:
    """回滚时恢复旧行情源标识，不修改具体合约代码。"""

    op.execute(
        """
        UPDATE instrument_market_data_mapping
        SET data_source = 'YML_FEEDHUB',
            updated_at = CURRENT_TIMESTAMP
        WHERE data_source = 'YMM_LIVE_DATA'
        """
    )
