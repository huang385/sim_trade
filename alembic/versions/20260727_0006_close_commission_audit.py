"""增加实际手续费快照和平仓成交逐笔持仓审计

Revision ID: 20260727_0006
Revises: 20260724_0005
Create Date: 2026-07-27
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260727_0006"
down_revision: Union[str, None] = "20260724_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """回填手续费快照，扩展冻结分配并建立平仓成交逐笔明细表。"""

    # 订单级快照供开仓成交按实际成交价重算手续费；普通 CLOSE 的每条
    # 持仓分配还会保存更细的平今/平昨参数快照。
    op.add_column(
        "orders",
        sa.Column("commission_type", sa.String(32), nullable=True),
    )
    op.add_column(
        "orders",
        sa.Column(
            "commission_parameter",
            sa.Numeric(24, 12),
            nullable=True,
        ),
    )
    op.add_column(
        "orders",
        sa.Column(
            "commission_contract_multiplier",
            sa.Numeric(24, 6),
            nullable=True,
        ),
    )
    op.execute(
        """
        UPDATE orders AS o
        SET commission_type = f.commission_type,
            commission_parameter = CASE
                WHEN o.offset_flag = 'OPEN' THEN f.open_commission
                WHEN o.offset_flag = 'CLOSE_TODAY'
                    THEN f.close_today_commission
                WHEN o.offset_flag IN ('CLOSE', 'CLOSE_YESTERDAY')
                    THEN f.close_commission
                ELSE NULL
            END,
            commission_contract_multiplier = i.contract_multiplier
        FROM fee_rule AS f, instrument AS i
        WHERE f.exchange_id = o.exchange_id
          AND f.symbol = o.symbol
          AND i.exchange_id = o.exchange_id
          AND i.symbol = o.symbol
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM orders
                WHERE commission_type IS NULL
                   OR commission_parameter IS NULL
                   OR commission_contract_multiplier IS NULL
                   OR commission_parameter < 0
                   OR commission_contract_multiplier <= 0
            ) THEN
                RAISE EXCEPTION
                    '订单手续费规则快照无法安全回填，请先修复缺失参考数据';
            END IF;
        END
        $$;
        """
    )

    for column in (
        "commission_type",
        "commission_parameter",
        "commission_contract_multiplier",
    ):
        op.alter_column("orders", column, nullable=False)
    op.create_check_constraint(
        "ck_order_commission_parameter_nonnegative",
        "orders",
        "commission_parameter >= 0",
    )
    op.create_check_constraint(
        "ck_order_commission_multiplier_positive",
        "orders",
        "commission_contract_multiplier > 0",
    )

    # 先保存迁移前各账户活动平仓订单冻结手续费总额，后续按正确的
    # 今/昨 Allocation 重算后，用差额同步修正账户冻结和可用资金。
    op.execute(
        """
        CREATE TEMP TABLE _old_close_frozen_commission
        ON COMMIT DROP AS
        SELECT account_id, SUM(frozen_commission)::numeric(24, 6) AS amount
        FROM orders
        WHERE offset_flag IN ('CLOSE', 'CLOSE_TODAY', 'CLOSE_YESTERDAY')
        GROUP BY account_id
        """
    )

    allocation_columns = (
        sa.Column("resolved_offset_flag", sa.String(32), nullable=True),
        sa.Column("commission_type", sa.String(32), nullable=True),
        sa.Column(
            "commission_parameter",
            sa.Numeric(24, 12),
            nullable=True,
        ),
        sa.Column(
            "commission_contract_multiplier",
            sa.Numeric(24, 6),
            nullable=True,
        ),
        sa.Column(
            "original_frozen_commission",
            sa.Numeric(24, 6),
            nullable=True,
        ),
        sa.Column(
            "remaining_frozen_commission",
            sa.Numeric(24, 6),
            nullable=True,
        ),
        sa.Column(
            "consumed_commission",
            sa.Numeric(24, 6),
            nullable=True,
        ),
        sa.Column(
            "released_commission",
            sa.Numeric(24, 6),
            nullable=True,
        ),
    )
    for column in allocation_columns:
        op.add_column("position_freeze_allocation", column)

    # 普通 CLOSE 必须根据原开仓交易日与订单交易日判断今昨，不能统一
    # 回填为平昨。未来日期或缺失关联数据都会在后续检查中明确失败。
    op.execute(
        """
        UPDATE position_freeze_allocation AS a
        SET resolved_offset_flag = CASE
                WHEN o.offset_flag = 'CLOSE_TODAY' THEN 'CLOSE_TODAY'
                WHEN o.offset_flag = 'CLOSE_YESTERDAY'
                    THEN 'CLOSE_YESTERDAY'
                WHEN o.offset_flag = 'CLOSE'
                     AND pd.open_trading_day = o.trading_day
                    THEN 'CLOSE_TODAY'
                WHEN o.offset_flag = 'CLOSE'
                     AND pd.open_trading_day < o.trading_day
                    THEN 'CLOSE_YESTERDAY'
                ELSE NULL
            END,
            commission_type = f.commission_type,
            commission_parameter = CASE
                WHEN (
                    o.offset_flag = 'CLOSE_TODAY'
                    OR (
                        o.offset_flag = 'CLOSE'
                        AND pd.open_trading_day = o.trading_day
                    )
                ) THEN f.close_today_commission
                ELSE f.close_commission
            END,
            commission_contract_multiplier = i.contract_multiplier
        FROM orders AS o,
             position_detail AS pd,
             fee_rule AS f,
             instrument AS i
        WHERE o.order_id = a.order_id
          AND pd.position_detail_id = a.position_detail_id
          AND f.exchange_id = o.exchange_id
          AND f.symbol = o.symbol
          AND i.exchange_id = o.exchange_id
          AND i.symbol = o.symbol
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM position_freeze_allocation
                WHERE resolved_offset_flag IS NULL
                   OR commission_type IS NULL
                   OR commission_parameter IS NULL
                   OR commission_contract_multiplier IS NULL
                   OR commission_parameter < 0
                   OR commission_contract_multiplier <= 0
            ) THEN
                RAISE EXCEPTION
                    '历史平仓Allocation无法安全判断今昨或手续费规则，请先修复数据';
            END IF;
        END
        $$;
        """
    )

    # 手续费计算对数量线性。original 使用完整冻结量重算；remaining
    # 和 consumed 分别按自身数量计算，released 吸收六位量化尾差，
    # 从而保证四字段严格守恒。
    op.execute(
        """
        UPDATE position_freeze_allocation AS a
        SET original_frozen_commission = ROUND(
                CASE
                    WHEN a.commission_type = 'BY_VOLUME'
                        THEN a.original_frozen_volume
                             * a.commission_parameter
                    WHEN a.commission_type = 'BY_AMOUNT'
                        THEN o.limit_price
                             * a.original_frozen_volume
                             * a.commission_contract_multiplier
                             * a.commission_parameter
                    ELSE NULL
                END,
                6
            ),
            remaining_frozen_commission = ROUND(
                CASE
                    WHEN a.commission_type = 'BY_VOLUME'
                        THEN a.remaining_frozen_volume
                             * a.commission_parameter
                    WHEN a.commission_type = 'BY_AMOUNT'
                        THEN o.limit_price
                             * a.remaining_frozen_volume
                             * a.commission_contract_multiplier
                             * a.commission_parameter
                    ELSE NULL
                END,
                6
            ),
            consumed_commission = ROUND(
                CASE
                    WHEN a.commission_type = 'BY_VOLUME'
                        THEN a.consumed_volume * a.commission_parameter
                    WHEN a.commission_type = 'BY_AMOUNT'
                        THEN o.limit_price
                             * a.consumed_volume
                             * a.commission_contract_multiplier
                             * a.commission_parameter
                    ELSE NULL
                END,
                6
            )
        FROM orders AS o
        WHERE o.order_id = a.order_id
        """
    )
    op.execute(
        """
        UPDATE position_freeze_allocation
        SET released_commission = ROUND(
            original_frozen_commission
            - remaining_frozen_commission
            - consumed_commission,
            6
        )
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM position_freeze_allocation
                WHERE original_frozen_commission IS NULL
                   OR remaining_frozen_commission IS NULL
                   OR consumed_commission IS NULL
                   OR released_commission IS NULL
                   OR original_frozen_commission < 0
                   OR remaining_frozen_commission < 0
                   OR consumed_commission < 0
                   OR released_commission < 0
            ) THEN
                RAISE EXCEPTION
                    '历史平仓Allocation手续费金额无法安全回填';
            END IF;
        END
        $$;
        """
    )

    # 订单剩余冻结手续费以 Allocation 为事实重建；账户同步调整对应
    # 差额，确保普通 CLOSE 跨今昨后冻结资源仍与订单完全一致。
    op.execute(
        """
        UPDATE orders AS o
        SET frozen_commission = x.amount
        FROM (
            SELECT order_id,
                   SUM(remaining_frozen_commission)::numeric(24, 6) AS amount
            FROM position_freeze_allocation
            GROUP BY order_id
        ) AS x
        WHERE x.order_id = o.order_id
        """
    )
    op.execute(
        """
        CREATE TEMP TABLE _new_close_frozen_commission
        ON COMMIT DROP AS
        SELECT account_id, SUM(frozen_commission)::numeric(24, 6) AS amount
        FROM orders
        WHERE offset_flag IN ('CLOSE', 'CLOSE_TODAY', 'CLOSE_YESTERDAY')
        GROUP BY account_id
        """
    )
    op.execute(
        """
        UPDATE account AS a
        SET frozen_commission = ROUND(
                a.frozen_commission + n.amount - o.amount,
                6
            ),
            available_cash = ROUND(
                a.available_cash - n.amount + o.amount,
                6
            )
        FROM _new_close_frozen_commission AS n
        JOIN _old_close_frozen_commission AS o
          ON o.account_id = n.account_id
        WHERE a.account_id = n.account_id
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM account
                WHERE frozen_commission < 0
                   OR available_cash < 0
            ) THEN
                RAISE EXCEPTION
                    '重算平仓手续费后账户资金不足，请先处理活动订单';
            END IF;
        END
        $$;
        """
    )

    for column in (
        "resolved_offset_flag",
        "commission_type",
        "commission_parameter",
        "commission_contract_multiplier",
        "original_frozen_commission",
        "remaining_frozen_commission",
        "consumed_commission",
        "released_commission",
    ):
        op.alter_column(
            "position_freeze_allocation",
            column,
            nullable=False,
        )
    op.create_check_constraint(
        "ck_position_freeze_resolved_offset",
        "position_freeze_allocation",
        "resolved_offset_flag IN ('CLOSE_TODAY', 'CLOSE_YESTERDAY')",
    )
    op.create_check_constraint(
        "ck_position_freeze_commission_parameter_nonnegative",
        "position_freeze_allocation",
        "commission_parameter >= 0",
    )
    op.create_check_constraint(
        "ck_position_freeze_commission_multiplier_positive",
        "position_freeze_allocation",
        "commission_contract_multiplier > 0",
    )
    op.create_check_constraint(
        "ck_position_freeze_commission_nonnegative",
        "position_freeze_allocation",
        "original_frozen_commission >= 0 "
        "AND remaining_frozen_commission >= 0 "
        "AND consumed_commission >= 0 "
        "AND released_commission >= 0",
    )
    op.create_check_constraint(
        "ck_position_freeze_commission_balance",
        "position_freeze_allocation",
        "original_frozen_commission = remaining_frozen_commission "
        "+ consumed_commission + released_commission",
    )

    op.create_table(
        "trade_position_allocation",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "trade_position_allocation_id",
            sa.String(64),
            nullable=False,
        ),
        sa.Column("trade_id", sa.String(64), nullable=False),
        sa.Column("order_id", sa.String(64), nullable=False),
        sa.Column("allocation_id", sa.String(64), nullable=False),
        sa.Column("position_id", sa.String(64), nullable=False),
        sa.Column("position_detail_id", sa.String(64), nullable=False),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("order_book_id", sa.String(64), nullable=False),
        sa.Column("exchange_id", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("resolved_offset_flag", sa.String(32), nullable=False),
        sa.Column("open_trading_day", sa.Date(), nullable=False),
        sa.Column("close_trading_day", sa.Date(), nullable=False),
        sa.Column("open_price", sa.Numeric(24, 6), nullable=False),
        sa.Column("close_price", sa.Numeric(24, 6), nullable=False),
        sa.Column("close_volume", sa.Integer(), nullable=False),
        sa.Column("released_margin", sa.Numeric(24, 6), nullable=False),
        sa.Column("commission", sa.Numeric(24, 6), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(24, 6), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_trade_position_allocation"),
        sa.UniqueConstraint(
            "trade_position_allocation_id",
            name="uq_trade_position_allocation_id",
        ),
        sa.UniqueConstraint(
            "trade_id",
            "allocation_id",
            name="uq_trade_position_trade_allocation",
        ),
        sa.CheckConstraint(
            "resolved_offset_flag IN ('CLOSE_TODAY', 'CLOSE_YESTERDAY')",
            name="ck_trade_position_resolved_offset",
        ),
        sa.CheckConstraint(
            "close_volume > 0",
            name="ck_trade_position_close_volume_positive",
        ),
        sa.CheckConstraint(
            "released_margin >= 0",
            name="ck_trade_position_margin_nonnegative",
        ),
        sa.CheckConstraint(
            "commission >= 0",
            name="ck_trade_position_commission_nonnegative",
        ),
    )
    op.create_index(
        "ix_trade_position_allocation_trade_id",
        "trade_position_allocation",
        ["trade_id"],
    )
    op.create_index(
        "ix_trade_position_order_id",
        "trade_position_allocation",
        ["order_id"],
    )
    op.create_index(
        "ix_trade_position_allocation_position_id",
        "trade_position_allocation",
        ["position_id"],
    )
    op.create_index(
        "ix_trade_position_position_detail_id",
        "trade_position_allocation",
        ["position_detail_id"],
    )
    op.create_index(
        "ix_trade_position_allocation_account_id",
        "trade_position_allocation",
        ["account_id"],
    )


def downgrade() -> None:
    """移除平仓成交明细、Allocation手续费资源和订单手续费快照。"""

    op.drop_table("trade_position_allocation")
    for constraint in (
        "ck_position_freeze_commission_balance",
        "ck_position_freeze_commission_nonnegative",
        "ck_position_freeze_commission_multiplier_positive",
        "ck_position_freeze_commission_parameter_nonnegative",
        "ck_position_freeze_resolved_offset",
    ):
        op.drop_constraint(
            constraint,
            "position_freeze_allocation",
            type_="check",
        )
    for column in (
        "released_commission",
        "consumed_commission",
        "remaining_frozen_commission",
        "original_frozen_commission",
        "commission_contract_multiplier",
        "commission_parameter",
        "commission_type",
        "resolved_offset_flag",
    ):
        op.drop_column("position_freeze_allocation", column)

    op.drop_constraint(
        "ck_order_commission_multiplier_positive",
        "orders",
        type_="check",
    )
    op.drop_constraint(
        "ck_order_commission_parameter_nonnegative",
        "orders",
        type_="check",
    )
    op.drop_column("orders", "commission_contract_multiplier")
    op.drop_column("orders", "commission_parameter")
    op.drop_column("orders", "commission_type")
