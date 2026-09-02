from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Sequence

from redis.exceptions import RedisError
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.common.exceptions import AuthorizationError
from app.common.decimal_utils import quantize_money
from app.common.time_utils import utc_now
from app.core.config import settings
from app.enums.auth_enums import UserRole
from app.enums.order_enums import OrderStatus
from app.infrastructure.realtime_pnl_store import RealtimePnlStore
from app.models.account import Account
from app.models.order import Order
from app.models.position import Position
from app.models.position_detail import PositionDetail
from app.models.trade import Trade
from app.repositories.outbox_repository import OutboxRepository
from app.schemas.account_schema import AccountResponse
from app.schemas.order_schema import OrderResponse
from app.schemas.pnl_schema import (
    AccountRealtimePnl,
    AccountRealtimePnlResponse,
    PositionRealtimePnl,
    PositionRealtimePnlResponse,
)
from app.schemas.position_schema import PositionResponse
from app.schemas.trade_schema import TradeResponse
from app.services.realtime_pnl_query_service import RealtimePnlQueryService
from app.services.account_risk_state_service import AccountRiskStateService
from app.services.account_valuation_calculator import AccountValuationCalculator
from app.realtime.subscription_service import RealtimeUserIdentity


ACTIVE_ORDER_STATUSES = (
    OrderStatus.ACCEPTED.value,
    OrderStatus.PARTIALLY_FILLED.value,
)
RISK_QUANT = Decimal("0.00000001")

# 保证金调整是PnL Worker自产事实：Worker先写Hash、同周期内调整事务再产生
# 新版本，下一轮对账必然覆盖。严格快照按此口径排除，避免每个调整周期内
# 出现最长60秒的周期性断连；其它真实业务事实（成交、开平仓、出入金、日终
# 结算）仍严格比对。
WS_EXCLUDED_FACT_REASONS = (
    "OPTION_MARGIN_ADJUSTMENT",
    "OPTION_ORDER_MARGIN_ADJUSTMENT",
)

# 兜底：排除口径生效时，最新非调整事实超过该秒数仍未覆盖，说明Worker真正
# 故障而非正常追赶窗口，仍然拒绝快照，防止静默返回过期数据。
WS_FACT_STALE_GUARD_SECONDS = 120


def _json_value(value: Any) -> Any:
    """快照金额固定输出Decimal字符串，日期时间输出ISO字符串。"""

    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date,)):
        return value.isoformat()
    return value


class SnapshotService:
    """用集合查询和Redis批量读取构造多个账户的完整只读快照。"""

    def __init__(
        self,
        pnl_store: RealtimePnlStore,
        outbox_repository: OutboxRepository | None = None,
    ):
        self.pnl_store = pnl_store
        self.outbox_repository = outbox_repository or OutboxRepository()

    @staticmethod
    def _rows_by_account(rows: Sequence[Any]) -> dict[str, list[Any]]:
        result: dict[str, list[Any]] = defaultdict(list)
        for row in rows:
            result[row.account_id].append(row)
        return result

    @staticmethod
    def _fact_version_covers(applied: Any, current: Any) -> bool:
        """比较Outbox整数版本，不经过float或Lua double。"""

        applied_text = str(applied or "").strip()
        current_text = str(current or "0").strip()
        if not applied_text.isdigit() or not current_text.isdigit():
            return False
        applied_normalized = applied_text.lstrip("0") or "0"
        current_normalized = current_text.lstrip("0") or "0"
        if len(applied_normalized) != len(current_normalized):
            return len(applied_normalized) > len(current_normalized)
        return applied_normalized >= current_normalized

    def _guard_stale_fact_errors(
        self,
        db: Session,
        fact_errors: list[tuple[str, str]],
        *,
        exclude_fact_reasons: Sequence[str],
        stale_guard_seconds: int,
    ) -> list[tuple[str, str]]:
        """排除口径下只放行Worker正常追赶窗口，真正故障仍拒绝。

        最新非调整事实刚写入（未超过兜底阈值）时，允许Hash短暂落后；
        事实本身已超过阈值仍未覆盖，或该聚合没有任何非调整事实可对账，
        说明不是正常追赶而是Worker停摆，保留该错误继续拒绝快照。
        """

        if not exclude_fact_reasons or stale_guard_seconds <= 0:
            return fact_errors
        created_times = self.outbox_repository.list_latest_fact_created_times(
            db,
            account_ids=tuple(
                key for kind, key in fact_errors if kind == "ACCOUNT"
            ),
            position_ids=tuple(
                key for kind, key in fact_errors if kind == "POSITION"
            ),
            exclude_fact_reasons=exclude_fact_reasons,
        )
        cutoff = utc_now() - timedelta(seconds=stale_guard_seconds)
        return [
            error
            for error in fact_errors
            if error not in created_times or created_times[error] <= cutoff
        ]

    @staticmethod
    def _risk_ratio(required_margin: Decimal, equity: Decimal) -> Decimal:
        if equity <= 0:
            return Decimal("0.00000000")
        return (required_margin / equity).quantize(
            RISK_QUANT,
            rounding=ROUND_HALF_UP,
        )

    @classmethod
    def _zero_position_account_values(cls, account: Account) -> dict[str, str]:
        """用同一数据库快照为无活动持仓账户构造安全的零持仓估值。

        Redis可能还残留最后一条持仓关闭前的浮盈，严格WebSocket快照不能
        使用它。账户现金、实际/冻结保证金和手续费继续取PostgreSQL事实，
        所有派生金额仍通过统一Decimal估值公式计算。
        """

        zero = Decimal("0")
        valuation = AccountValuationCalculator.calculate(
            cash_balance=Decimal(account.cash_balance),
            futures_unrealized_pnl=zero,
            long_option_market_value=zero,
            short_option_market_value=zero,
            used_margin=Decimal(account.used_margin),
            option_used_margin=Decimal(account.option_used_margin),
            option_realtime_required_margin=zero,
            frozen_margin=Decimal(account.frozen_margin),
            frozen_cash=Decimal(account.frozen_cash),
            frozen_commission=Decimal(account.frozen_commission),
            option_collateral_ratio=settings.option_collateral_ratio,
        )
        risk_state = AccountRiskStateService.preserve_for_local_update(
            account.risk_state,
            margin_deficit=valuation.risk_available_cash < zero,
        )
        model = AccountRealtimePnl(
            account_id=account.account_id,
            cumulative_unrealized_pnl=quantize_money(zero),
            daily_position_pnl=quantize_money(zero),
            daily_close_pnl=Decimal(account.daily_close_pnl),
            daily_commission=Decimal(account.daily_commission),
            daily_pnl=quantize_money(
                Decimal(account.daily_close_pnl)
                - Decimal(account.daily_commission)
            ),
            cumulative_net_pnl=Decimal(
                getattr(account, "cumulative_net_pnl", Decimal("0"))
            ),
            equity=valuation.equity,
            available_cash=valuation.available_cash,
            futures_unrealized_pnl=quantize_money(zero),
            option_realtime_required_margin=quantize_money(zero),
            long_option_market_value=quantize_money(zero),
            short_option_market_value=quantize_money(zero),
            net_option_market_value=quantize_money(zero),
            risk_available_cash=valuation.risk_available_cash,
            risk_state=risk_state,
            risk_ratio=cls._risk_ratio(
                valuation.effective_required_margin,
                valuation.equity,
            ),
            updated_at=account.updated_at,
            data_source="POSTGRES_ZERO_POSITION",
            source_account_fact_version="0",
            trading_day=getattr(account, "trading_day", None),
        )
        return {
            key: (
                format(value, "f")
                if isinstance(value, Decimal)
                else value.isoformat()
                if isinstance(value, datetime)
                else str(value)
            )
            for key, value in model.model_dump(mode="python").items()
            if value is not None
        }

    def build(
        self,
        db: Session,
        account_ids: set[str],
        *,
        identity: RealtimeUserIdentity | None = None,
        require_realtime_consistency: bool = False,
        exclude_fact_reasons: Sequence[str] = (),
        stale_guard_seconds: int = WS_FACT_STALE_GUARD_SECONDS,
    ) -> dict[str, Any]:
        """固定集合SQL读取账户、订单、成交、持仓、明细和事实版本。

        WebSocket严格模式在PostgreSQL使用只读REPEATABLE READ，并要求
        Redis实时Hash完整；严格模式额外执行一次Outbox版本集合查询，普通
        HTTP调用仍允许回退数据库最近快照。
        """

        if not account_ids:
            return {"generated_at": utc_now().isoformat(), "accounts": []}
        if require_realtime_consistency:
            bind = db.get_bind()
            if bind.dialect.name == "postgresql":
                # 必须在本Session第一条SQL前设置隔离级别，随后五次集合查询
                # 才会看到同一个MVCC快照，避免旧Account与新Position混合。
                db.connection(
                    execution_options={"isolation_level": "REPEATABLE READ"}
                )
                db.execute(text("SET TRANSACTION READ ONLY"))
        ids = tuple(sorted(account_ids))
        account_statement = select(Account).where(
            Account.account_id.in_(ids)
        )
        if identity is not None and identity.role != UserRole.ADMIN.value:
            account_statement = account_statement.where(
                Account.user_id == identity.user_id
            )
        accounts = list(
            db.scalars(
                account_statement.order_by(Account.id)
            ).all()
        )
        authorized_ids = tuple(account.account_id for account in accounts)
        if set(authorized_ids) != set(ids):
            raise AuthorizationError(
                "无权读取目标交易账户快照",
                error_code="WS_ACCOUNT_ACCESS_DENIED",
            )
        positions = list(
            db.scalars(
                select(Position)
                .where(
                    Position.account_id.in_(ids),
                    Position.total_volume > 0,
                )
                .order_by(Position.id)
            ).all()
        )
        orders = list(
            db.scalars(
                select(Order)
                .where(
                    Order.account_id.in_(ids),
                    Order.status.in_(ACTIVE_ORDER_STATUSES),
                    Order.remaining_volume > 0,
                )
                .order_by(Order.id)
            ).all()
        )
        # 当日成交使用账户自己的交易日匹配，不按服务器日期推算交易日。
        trades = list(
            db.scalars(
                select(Trade)
                .join(Account, Account.account_id == Trade.account_id)
                .where(
                    Trade.account_id.in_(ids),
                    Trade.trading_day == Account.trading_day,
                )
                .order_by(Trade.id)
            ).all()
        )
        position_ids = tuple(position.position_id for position in positions)
        active_account_ids = {
            position.account_id for position in positions
        }
        details = (
            list(
                db.scalars(
                    select(PositionDetail)
                    .where(
                        PositionDetail.position_id.in_(position_ids),
                        PositionDetail.remaining_volume > 0,
                    )
                    .order_by(PositionDetail.id)
                ).all()
            )
            if position_ids
            else []
        )
        # 与账户、持仓读取处于同一个REPEATABLE READ事务；使用一次集合SQL
        # 取得当前事实Outbox版本，不产生逐账户或逐持仓查询。
        current_fact_versions = (
            self.outbox_repository.list_latest_fact_versions(
                db,
                account_ids=authorized_ids,
                position_ids=position_ids,
                exclude_fact_reasons=exclude_fact_reasons,
            )
            if require_realtime_consistency
            else {}
        )

        account_versions: dict[str, str] = {}
        position_versions: dict[str, str] = {}
        dirty_account_facts: set[str] = set()
        dirty_account_structures: set[str] = set()
        try:
            if require_realtime_consistency:
                if position_ids:
                    (
                        account_values,
                        position_values,
                        account_versions,
                        position_versions,
                        dirty_account_facts,
                        dirty_account_structures,
                    ) = (
                        self.pnl_store
                        .get_accounts_with_positions_and_versions(
                            account_ids=active_account_ids,
                            position_ids=position_ids,
                        )
                    )
                else:
                    # PostgreSQL已经确认没有活动持仓时，旧Redis Hash既不
                    # 参与计算也不应成为可用性依赖；下方直接用同一MVCC
                    # 事务中的账户事实构造零持仓估值。
                    account_values = {}
                    position_values = {}
            else:
                account_values, position_values = (
                    self.pnl_store.get_accounts_with_positions(
                        account_ids=ids,
                        position_ids=position_ids,
                    )
                )
        except RedisError:
            if require_realtime_consistency:
                raise
            account_values, position_values = {}, {}

        if require_realtime_consistency:
            account_by_id = {
                account.account_id: account for account in accounts
            }
            position_by_id = {
                position.position_id: position for position in positions
            }
            mismatched_accounts = [
                account_id
                for account_id, values in account_values.items()
                if values
                and not RealtimePnlQueryService._snapshot_matches_trading_day(
                    values, account_by_id[account_id]
                )
            ]
            mismatched_positions = [
                position_id
                for position_id, values in position_values.items()
                if values
                and not RealtimePnlQueryService._snapshot_matches_trading_day(
                    values, position_by_id[position_id]
                )
            ]
            if mismatched_accounts or mismatched_positions:
                raise RedisError("WebSocket实时快照交易日不一致")
            # 有活动持仓时，账户和每条持仓都必须具有完整实时Hash。无持仓
            # 账户没有行情派生值，使用同一数据库快照中的资金事实是安全的。
            missing_positions = [
                position_id
                for position_id in position_ids
                if not position_values.get(position_id)
            ]
            missing_accounts = [
                account_id
                for account_id in ids
                if account_id in active_account_ids
                and not account_values.get(account_id)
            ]
            if missing_positions or missing_accounts:
                raise RedisError("WebSocket实时快照Hash缺失")
            try:
                for values in account_values.values():
                    if values:
                        AccountRealtimePnl.model_validate(values)
                for values in position_values.values():
                    if values:
                        PositionRealtimePnl.model_validate(values)
            except (TypeError, ValueError) as exc:
                raise RedisError("WebSocket实时快照Hash不完整") from exc

            # Hash内版本和独立最新版本索引由PnL单写者在同一Lua脚本写入。
            # 格式完整但版本落后的Hash仍必须失败，不能推进Stream游标。
            version_errors: list[str] = []
            for account_id in active_account_ids:
                actual = str(
                    account_values.get(account_id, {}).get(
                        "realtime_snapshot_version",
                        "",
                    )
                )
                expected = account_versions.get(account_id, "")
                if not expected.isdigit() or actual != expected:
                    version_errors.append(f"account:{account_id}")
            for position_id in position_ids:
                actual = str(
                    position_values.get(position_id, {}).get(
                        "realtime_snapshot_version",
                        "",
                    )
                )
                expected = position_versions.get(position_id, "")
                if not expected.isdigit() or actual != expected:
                    version_errors.append(f"position:{position_id}")
            if version_errors:
                raise RedisError("WebSocket实时快照版本不一致")

            if dirty_account_facts or dirty_account_structures:
                # Dirty清理由Worker在Lua快照成功写入后按版本CAS完成。只要
                # 账户仍有资金或结构Dirty，就不能证明本次Hash覆盖最新事实。
                raise RedisError("WebSocket实时快照仍有未处理业务事实")

            fact_errors: list[tuple[str, str]] = []
            for account_id in active_account_ids:
                if not self._fact_version_covers(
                    account_values[account_id].get(
                        "source_account_fact_version"
                    ),
                    current_fact_versions.get(
                        ("ACCOUNT", account_id),
                        "0",
                    ),
                ):
                    fact_errors.append(("ACCOUNT", account_id))
            for position_id in position_ids:
                if not self._fact_version_covers(
                    position_values[position_id].get(
                        "source_position_fact_version"
                    ),
                    current_fact_versions.get(
                        ("POSITION", position_id),
                        "0",
                    ),
                ):
                    fact_errors.append(("POSITION", position_id))
            if fact_errors:
                fact_errors = self._guard_stale_fact_errors(
                    db,
                    fact_errors,
                    exclude_fact_reasons=exclude_fact_reasons,
                    stale_guard_seconds=stale_guard_seconds,
                )
            if fact_errors:
                raise RedisError("WebSocket实时PnL尚未覆盖最新业务事实")

            # 没有活动持仓的账户绝不能复用残留Redis账户Hash。使用本次
            # REPEATABLE READ事务中的账户事实构造零持仓估值，关闭最后一条
            # 持仓后即使PnL Worker延迟或后续无行情也不会保留旧浮盈。
            for account in accounts:
                if account.account_id not in active_account_ids:
                    account_values[account.account_id] = (
                        self._zero_position_account_values(account)
                    )

        positions_by_account = self._rows_by_account(positions)
        orders_by_account = self._rows_by_account(orders)
        trades_by_account = self._rows_by_account(trades)
        details_by_position: dict[str, list[PositionDetail]] = defaultdict(list)
        for detail in details:
            details_by_position[detail.position_id].append(detail)

        snapshots: list[dict[str, Any]] = []
        for account in accounts:
            account_id = account.account_id
            account_realtime = account_values.get(account_id, {})
            account_pnl = RealtimePnlQueryService._account_pnl_response(
                values=account_realtime,
                account=account,
            )
            position_snapshots: list[dict[str, Any]] = []
            for position in positions_by_account.get(account_id, []):
                realtime_values = position_values.get(
                    position.position_id,
                    {},
                )
                pnl = RealtimePnlQueryService._position_pnl_response(
                    values=realtime_values,
                    position=position,
                )
                position_snapshots.append(
                    {
                        "position": PositionResponse.model_validate(
                            position
                        ).model_dump(mode="json"),
                        "pnl": PositionRealtimePnlResponse.model_validate(
                            pnl
                        ).model_dump(mode="json"),
                        "valuation": dict(realtime_values),
                        "details": [
                            {
                                "position_detail_id": detail.position_detail_id,
                                "open_trade_id": detail.open_trade_id,
                                "open_trading_day": detail.open_trading_day.isoformat(),
                                "open_price": _json_value(detail.open_price),
                                "pnl_base_price": _json_value(detail.pnl_base_price),
                                "original_volume": detail.original_volume,
                                "remaining_volume": detail.remaining_volume,
                                "frozen_volume": detail.frozen_volume,
                                "remaining_margin": _json_value(
                                    detail.remaining_margin
                                ),
                                "status": detail.status,
                                "updated_at": detail.updated_at.isoformat(),
                            }
                            for detail in details_by_position.get(
                                position.position_id,
                                [],
                            )
                        ],
                    }
                )
            snapshots.append(
                {
                    "account": AccountResponse.model_validate(
                        account
                    ).model_dump(mode="json"),
                    "pnl": AccountRealtimePnlResponse.model_validate(
                        account_pnl
                    ).model_dump(mode="json"),
                    "valuation": dict(account_realtime),
                    "active_orders": [
                        OrderResponse.model_validate(order).model_dump(
                            mode="json"
                        )
                        for order in orders_by_account.get(account_id, [])
                    ],
                    "today_trades": [
                        TradeResponse.model_validate(trade).model_dump(
                            mode="json"
                        )
                        for trade in trades_by_account.get(account_id, [])
                    ],
                    "positions": position_snapshots,
                }
            )
        return {
            "generated_at": utc_now().isoformat(),
            "realtime": bool(account_values or position_values),
            "accounts": snapshots,
        }
