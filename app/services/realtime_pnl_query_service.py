from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session
from redis.exceptions import RedisError

from app.common.exceptions import ResourceNotFoundError
from app.infrastructure.realtime_pnl_store import RealtimePnlStore
from app.repositories.account_repository import AccountRepository
from app.repositories.position_repository import PositionRepository
from app.schemas.pnl_schema import (
    AccountRealtimePnlResponse,
    PositionRealtimePnlResponse,
)


def _decimal(values: dict[str, str], name: str) -> Decimal:
    """把Redis十进制字符串还原为Decimal，禁止经过float损失精度。"""

    try:
        return Decimal(values[name])
    except (KeyError, InvalidOperation) as exc:
        raise ValueError(f"实时盈亏快照字段不合法: {name}") from exc


class RealtimePnlQueryService:
    """
    提供实时盈亏只读查询。

    Redis实时Hash存在时优先返回；快照尚未产生或已丢失时，回退到PostgreSQL最近
    一次成功持久化的字段，并通过data_source让调用方识别数据新鲜度。
    """

    def __init__(
        self,
        *,
        pnl_store: RealtimePnlStore,
        account_repository: AccountRepository | None = None,
        position_repository: PositionRepository | None = None,
    ):
        self.pnl_store = pnl_store
        self.account_repository = (
            account_repository or AccountRepository()
        )
        self.position_repository = (
            position_repository or PositionRepository()
        )

    def get_account(
        self,
        db: Session,
        account_id: str,
    ) -> AccountRealtimePnlResponse:
        normalized = account_id.strip()
        try:
            values = self.pnl_store.get_account(normalized)
        except RedisError:
            # 查询接口允许在Redis短暂不可用时返回最近一次持久化结果；
            # 不把PostgreSQL事实数据误报为实时数据。
            values = {}
        if values:
            return AccountRealtimePnlResponse(
                account_id=values["account_id"],
                unrealized_pnl=_decimal(
                    values,
                    "cumulative_unrealized_pnl",
                ),
                daily_position_pnl=_decimal(
                    values,
                    "daily_position_pnl",
                ),
                daily_close_pnl=_decimal(values, "daily_close_pnl"),
                daily_commission=_decimal(values, "daily_commission"),
                daily_pnl=_decimal(values, "daily_pnl"),
                equity=_decimal(values, "equity"),
                available_cash=_decimal(values, "available_cash"),
                risk_ratio=_decimal(values, "risk_ratio"),
                updated_at=values["updated_at"],
                data_source="REDIS_REALTIME",
            )

        account = self.account_repository.get_by_account_id(
            db,
            normalized,
        )
        if account is None:
            raise ResourceNotFoundError(
                "账户不存在",
                error_code="ACCOUNT_NOT_FOUND",
            )
        return AccountRealtimePnlResponse(
            account_id=account.account_id,
            unrealized_pnl=account.unrealized_pnl,
            daily_position_pnl=account.daily_position_pnl,
            daily_close_pnl=account.daily_close_pnl,
            daily_commission=account.daily_commission,
            daily_pnl=account.daily_pnl,
            equity=account.equity,
            available_cash=account.available_cash,
            risk_ratio=account.risk_ratio,
            updated_at=account.updated_at,
            data_source="POSTGRES_SNAPSHOT",
        )

    def get_position(
        self,
        db: Session,
        position_id: str,
    ) -> PositionRealtimePnlResponse:
        normalized = position_id.strip()
        try:
            values = self.pnl_store.get_position(normalized)
        except RedisError:
            values = {}
        if values:
            return PositionRealtimePnlResponse(
                position_id=values["position_id"],
                account_id=values["account_id"],
                exchange_id=values["exchange_id"],
                symbol=values["symbol"],
                direction=values["direction"],
                mark_price=_decimal(values, "mark_price"),
                unrealized_pnl=_decimal(
                    values,
                    "cumulative_unrealized_pnl",
                ),
                daily_position_pnl=_decimal(
                    values,
                    "daily_position_pnl",
                ),
                event_time=values["event_time"],
                source_event_id=values["source_event_id"],
                updated_at=values["updated_at"],
                data_source="REDIS_REALTIME",
            )

        position = self.position_repository.get_by_position_id(
            db,
            normalized,
        )
        if position is None:
            raise ResourceNotFoundError(
                "持仓不存在",
                error_code="POSITION_NOT_FOUND",
            )
        # PostgreSQL当前不保存mark_price和行情事件编号，回退响应明确返回空值，
        # 避免把未知行情伪装成0价格。
        return PositionRealtimePnlResponse(
            position_id=position.position_id,
            account_id=position.account_id,
            exchange_id=position.exchange_id,
            symbol=position.symbol,
            direction=position.direction,
            mark_price=None,
            unrealized_pnl=position.unrealized_pnl,
            daily_position_pnl=position.daily_position_pnl,
            event_time=None,
            source_event_id=None,
            updated_at=position.updated_at,
            data_source="POSTGRES_SNAPSHOT",
        )
