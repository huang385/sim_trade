"""修复期权风险一致性并回填真实合约乘数。

Revision ID: 20260803_0012
Revises: 20260730_0011
"""

from alembic import op
import sqlalchemy as sa


revision = "20260803_0012"
down_revision = "20260730_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 活动期权卖出开仓订单需要直接建立标的行情依赖索引，避免每500ms
    # 根据Instrument关系扫描全部活动订单。
    op.add_column(
        "orders",
        sa.Column("underlying_order_book_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "orders",
        sa.Column("underlying_exchange_id", sa.String(32), nullable=True),
    )
    op.add_column(
        "orders",
        sa.Column("underlying_symbol", sa.String(64), nullable=True),
    )
    op.execute(
        """
        UPDATE orders AS o
        SET underlying_order_book_id = u.order_book_id,
            underlying_exchange_id = u.exchange_id,
            underlying_symbol = u.symbol
        FROM instrument AS option_i
        JOIN instrument AS u
          ON u.id = option_i.underlying_instrument_id
        WHERE option_i.order_book_id = o.order_book_id
          AND o.instrument_type IN ('FUTURES_OPTION', 'INDEX_OPTION')
        """
    )

    # Position保存最近一次可靠标记市值，使平仓事务能够扣减旧持仓市值，
    # 不再错误地使用本次成交额替代标记市值。
    op.add_column(
        "position",
        sa.Column(
            "option_market_value",
            sa.Numeric(24, 6),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_check_constraint(
        "ck_position_option_market_value_nonnegative",
        "position",
        "option_market_value >= 0",
    )

    # 0011曾把历史PositionDetail乘数统一写成1，并让Position保持NULL。
    # 修复迁移必须从对应Instrument回填；找不到合约时直接终止升级，禁止
    # 静默产生不可审计的错误历史数据。
    op.execute(
        """
        UPDATE position AS p
        SET multiplier_snapshot = i.contract_multiplier
        FROM instrument AS i
        WHERE i.order_book_id = p.order_book_id
        """
    )
    op.execute(
        """
        UPDATE position_detail AS pd
        SET multiplier_snapshot = i.contract_multiplier
        FROM instrument AS i
        WHERE i.order_book_id = pd.order_book_id
        """
    )
    # 个别旧集成测试数据库可能已经删除临时Instrument，但持仓成本和成交额
    # 仍能精确反推出乘数。该回退公式可审计且不使用固定值1；无法反推时
    # 后面的显式异常仍会中止整个事务。
    op.execute(
        """
        UPDATE position AS p
        SET multiplier_snapshot = ROUND(
            p.position_cost / (p.average_open_price * p.total_volume),
            6
        )
        WHERE p.multiplier_snapshot IS NULL
          AND p.position_cost > 0
          AND p.average_open_price > 0
          AND p.total_volume > 0
        """
    )
    op.execute(
        """
        UPDATE position_detail AS pd
        SET multiplier_snapshot = ROUND(
            t.turnover / (pd.open_price * pd.original_volume),
            6
        )
        FROM trade AS t
        WHERE t.trade_id = pd.open_trade_id
          AND NOT EXISTS (
              SELECT 1 FROM instrument AS i
              WHERE i.order_book_id = pd.order_book_id
          )
          AND t.turnover > 0
          AND pd.open_price > 0
          AND pd.original_volume > 0
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM position
                WHERE multiplier_snapshot IS NULL
                   OR multiplier_snapshot <= 0
            ) OR EXISTS (
                SELECT 1 FROM position_detail
                WHERE multiplier_snapshot IS NULL
                   OR multiplier_snapshot <= 0
            ) OR EXISTS (
                SELECT 1
                FROM position_detail AS pd
                WHERE NOT EXISTS (
                    SELECT 1 FROM instrument AS i
                    WHERE i.order_book_id = pd.order_book_id
                )
                  AND NOT EXISTS (
                    SELECT 1 FROM trade AS t
                    WHERE t.trade_id = pd.open_trade_id
                      AND t.turnover > 0
                      AND pd.open_price > 0
                      AND pd.original_volume > 0
                )
            ) THEN
                RAISE EXCEPTION
                    '历史持仓无法从Instrument或成交事实回填multiplier_snapshot';
            END IF;
        END $$
        """
    )
    # 期权市值必须在真实乘数完成回填后计算；否则旧Position的乘数为NULL时，
    # 会把可审计的历史期权市值错误写成0。
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
    op.alter_column(
        "position",
        "multiplier_snapshot",
        existing_type=sa.Numeric(24, 6),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_position_multiplier_positive",
        "position",
        "multiplier_snapshot > 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_position_multiplier_positive",
        "position",
        type_="check",
    )
    op.alter_column(
        "position",
        "multiplier_snapshot",
        existing_type=sa.Numeric(24, 6),
        nullable=True,
    )
    op.drop_constraint(
        "ck_position_option_market_value_nonnegative",
        "position",
        type_="check",
    )
    op.drop_column("position", "option_market_value")
    op.drop_column("orders", "underlying_symbol")
    op.drop_column("orders", "underlying_exchange_id")
    op.drop_column("orders", "underlying_order_book_id")
