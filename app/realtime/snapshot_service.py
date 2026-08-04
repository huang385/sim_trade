from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any, Sequence

from redis.exceptions import RedisError
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.common.exceptions import AuthorizationError
from app.common.time_utils import utc_now
from app.enums.auth_enums import UserRole
from app.enums.order_enums import OrderStatus
from app.infrastructure.realtime_pnl_store import RealtimePnlStore
from app.models.account import Account
from app.models.order import Order
from app.models.position import Position
from app.models.position_detail import PositionDetail
from app.models.trade import Trade
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
from app.realtime.subscription_service import RealtimeUserIdentity


ACTIVE_ORDER_STATUSES = (
    OrderStatus.ACCEPTED.value,
    OrderStatus.PARTIALLY_FILLED.value,
)


def _json_value(value: Any) -> Any:
    """快照金额固定输出Decimal字符串，日期时间输出ISO字符串。"""

    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date,)):
        return value.isoformat()
    return value


class SnapshotService:
    """用集合查询和Redis批量读取构造多个账户的完整只读快照。"""

    def __init__(self, pnl_store: RealtimePnlStore):
        self.pnl_store = pnl_store

    @staticmethod
    def _rows_by_account(rows: Sequence[Any]) -> dict[str, list[Any]]:
        result: dict[str, list[Any]] = defaultdict(list)
        for row in rows:
            result[row.account_id].append(row)
        return result

    def build(
        self,
        db: Session,
        account_ids: set[str],
        *,
        identity: RealtimeUserIdentity | None = None,
        require_realtime_consistency: bool = False,
    ) -> dict[str, Any]:
        """固定五次集合SQL读取账户、订单、成交、持仓和明细。

        WebSocket严格模式在PostgreSQL使用只读REPEATABLE READ，并要求
        Redis实时Hash完整；普通HTTP调用仍允许回退数据库最近快照。
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

        account_versions: dict[str, str] = {}
        position_versions: dict[str, str] = {}
        try:
            if require_realtime_consistency and position_ids:
                (
                    account_values,
                    position_values,
                    account_versions,
                    position_versions,
                ) = self.pnl_store.get_accounts_with_positions_and_versions(
                    account_ids=ids,
                    position_ids=position_ids,
                )
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
            # 有活动持仓时，账户和每条持仓都必须具有完整实时Hash。无持仓
            # 账户没有行情派生值，使用同一数据库快照中的资金事实是安全的。
            missing_positions = [
                position_id
                for position_id in position_ids
                if not position_values.get(position_id)
            ]
            active_account_ids = {
                position.account_id for position in positions
            }
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
