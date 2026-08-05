"""修复既有 RQData 期权合约的精确类型和标的关联。

Revision ID: 20260805_0016
Revises: 20260804_0015
"""

from alembic import op


revision = "20260805_0016"
down_revision = "20260804_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 股指期权依赖不可交易的指数标的。只在记录不存在时补齐，绝不覆盖
    # 管理员或后续参考数据同步已经维护的指数主数据。
    op.execute(
        """
        INSERT INTO instrument (
            order_book_id,
            symbol,
            exchange_id,
            instrument_name,
            product_id,
            market_type,
            instrument_type,
            underlying_instrument_id,
            option_type,
            strike_price,
            exercise_style,
            settlement_type,
            contract_multiplier,
            price_tick,
            min_volume,
            max_volume,
            listed_date,
            expire_date,
            last_trading_date,
            is_active,
            is_tradeable,
            data_source,
            synced_at,
            created_at,
            updated_at
        ) VALUES
            (
                '000300.XSHG', '000300.XSHG', 'XSHG', '沪深300指数',
                'CSI300', 'INDEX', 'INDEX', NULL, NULL, NULL, NULL, NULL,
                0, 0.01, 1, 1, NULL, NULL, NULL, true, false,
                'MIGRATION', now(), now(), now()
            ),
            (
                '000016.XSHG', '000016.XSHG', 'XSHG', '上证50指数',
                'SSE50', 'INDEX', 'INDEX', NULL, NULL, NULL, NULL, NULL,
                0, 0.01, 1, 1, NULL, NULL, NULL, true, false,
                'MIGRATION', now(), now(), now()
            ),
            (
                '000852.XSHG', '000852.XSHG', 'XSHG', '中证1000指数',
                'CSI1000', 'INDEX', 'INDEX', NULL, NULL, NULL, NULL, NULL,
                0, 0.01, 1, 1, NULL, NULL, NULL, true, false,
                'MIGRATION', now(), now(), now()
            )
        ON CONFLICT (order_book_id) DO NOTHING
        """
    )

    # 迁移前先证明每条目标记录都可解析、具有到期日并且能关联唯一标的。
    # 任一断言失败都会中止当前 Alembic 事务，不能留下部分修复结果。
    op.execute(
        r"""
        DO $$
        DECLARE
            invalid_count bigint;
        BEGIN
            SELECT count(*) INTO invalid_count
            FROM instrument
            WHERE data_source = 'RQDATA'
              AND market_type = 'OPTIONS'
              AND (
                    order_book_id !~ '^[A-Z]+[0-9]{4}(MS)?[CP][0-9]+(\.[0-9]+)?$'
                 OR expire_date IS NULL
                 OR (exchange_id = 'CFFEX' AND product_id NOT IN ('IO', 'HO', 'MO'))
              );
            IF invalid_count <> 0 THEN
                RAISE EXCEPTION
                    'option instrument repair found % invalid source rows',
                    invalid_count;
            END IF;

            WITH parsed AS (
                SELECT
                    option_row.id,
                    option_row.exchange_id,
                    CASE
                        WHEN option_row.exchange_id = 'CFFEX' THEN 'XSHG'
                        ELSE option_row.exchange_id
                    END AS underlying_exchange_id,
                    CASE option_row.product_id
                        WHEN 'IO' THEN '000300.XSHG'
                        WHEN 'HO' THEN '000016.XSHG'
                        WHEN 'MO' THEN '000852.XSHG'
                        ELSE substring(
                            upper(option_row.order_book_id)
                            FROM '^([A-Z]+[0-9]{4})'
                        )
                    END AS underlying_order_book_id
                FROM instrument AS option_row
                WHERE option_row.data_source = 'RQDATA'
                  AND option_row.market_type = 'OPTIONS'
            )
            SELECT count(*) INTO invalid_count
            FROM parsed
            LEFT JOIN instrument AS underlying
              ON underlying.exchange_id = parsed.underlying_exchange_id
             AND underlying.order_book_id = parsed.underlying_order_book_id
             AND underlying.instrument_type = CASE
                    WHEN parsed.exchange_id = 'CFFEX' THEN 'INDEX'
                    ELSE 'FUTURES'
                 END
            WHERE underlying.id IS NULL;
            IF invalid_count <> 0 THEN
                RAISE EXCEPTION
                    'option instrument repair found % missing underlyings',
                    invalid_count;
            END IF;
        END
        $$
        """
    )

    op.execute(
        r"""
        WITH parsed AS (
            SELECT
                option_row.id,
                option_row.exchange_id,
                CASE
                    WHEN option_row.exchange_id = 'CFFEX' THEN 'XSHG'
                    ELSE option_row.exchange_id
                END AS underlying_exchange_id,
                CASE option_row.product_id
                    WHEN 'IO' THEN '000300.XSHG'
                    WHEN 'HO' THEN '000016.XSHG'
                    WHEN 'MO' THEN '000852.XSHG'
                    ELSE substring(
                        upper(option_row.order_book_id)
                        FROM '^([A-Z]+[0-9]{4})'
                    )
                END AS underlying_order_book_id,
                CASE
                    WHEN upper(option_row.order_book_id)
                         ~ 'C[0-9]+(\.[0-9]+)?$'
                        THEN 'CALL'
                    WHEN upper(option_row.order_book_id)
                         ~ 'P[0-9]+(\.[0-9]+)?$'
                        THEN 'PUT'
                END AS option_type,
                substring(
                    upper(option_row.order_book_id)
                    FROM '[CP]([0-9]+(\.[0-9]+)?)$'
                )::numeric AS strike_price
            FROM instrument AS option_row
            WHERE option_row.data_source = 'RQDATA'
              AND option_row.market_type = 'OPTIONS'
        ), resolved AS (
            SELECT parsed.*, underlying.id AS underlying_instrument_id
            FROM parsed
            JOIN instrument AS underlying
              ON underlying.exchange_id = parsed.underlying_exchange_id
             AND underlying.order_book_id = parsed.underlying_order_book_id
             AND underlying.instrument_type = CASE
                    WHEN parsed.exchange_id = 'CFFEX' THEN 'INDEX'
                    ELSE 'FUTURES'
                 END
        )
        UPDATE instrument AS option_row
        SET instrument_type = CASE
                WHEN resolved.exchange_id = 'CFFEX'
                    THEN 'INDEX_OPTION'
                ELSE 'FUTURES_OPTION'
            END,
            underlying_instrument_id = resolved.underlying_instrument_id,
            option_type = resolved.option_type,
            strike_price = resolved.strike_price,
            exercise_style = CASE
                WHEN resolved.exchange_id = 'CFFEX'
                    THEN 'EUROPEAN'
                ELSE 'AMERICAN'
            END,
            settlement_type = CASE
                WHEN resolved.exchange_id = 'CFFEX'
                    THEN 'CASH'
                ELSE 'PHYSICAL'
            END,
            updated_at = now()
        FROM resolved
        WHERE option_row.id = resolved.id
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE instrument
        SET instrument_type = 'FUTURES',
            underlying_instrument_id = NULL,
            option_type = NULL,
            strike_price = NULL,
            exercise_style = NULL,
            settlement_type = NULL,
            updated_at = now()
        WHERE data_source = 'RQDATA'
          AND market_type = 'OPTIONS'
          AND instrument_type IN ('FUTURES_OPTION', 'INDEX_OPTION')
        """
    )
    op.execute(
        """
        DELETE FROM instrument
        WHERE data_source = 'MIGRATION'
          AND instrument_type = 'INDEX'
          AND order_book_id IN (
              '000300.XSHG',
              '000016.XSHG',
              '000852.XSHG'
          )
        """
    )
