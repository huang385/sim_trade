from decimal import Decimal, InvalidOperation

from redis.exceptions import RedisError
from sqlalchemy.orm import Session

from app.common.exceptions import ResourceNotFoundError
from app.infrastructure.realtime_pnl_store import RealtimePnlStore
from app.repositories.account_repository import AccountRepository
from app.repositories.position_repository import PositionRepository
from app.schemas.account_schema import AccountResponse
from app.schemas.pnl_schema import (
    AccountRealtimePnlResponse,
    AccountTradingSnapshotResponse,
    PositionRealtimePnlResponse,
    PositionTradingSnapshotResponse,
)
from app.schemas.position_schema import PositionResponse


def _decimal(values: dict[str, str], name: str) -> Decimal:
    """把Redis十进制字符串还原为Decimal，禁止经过float损失精度。"""

    try:
        return Decimal(values[name])
    except (KeyError, InvalidOperation) as exc:
        raise ValueError(f"实时盈亏快照字段不合法: {name}") from exc


class RealtimePnlQueryService:
    """
    提供实时盈亏只读查询。

    Redis实时Hash存在时优先返回；快照尚未产生或Redis暂时不可用时，回退到
    PostgreSQL最近一次成功持久化的数据，并通过data_source标记数据来源。
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

    @staticmethod
    def _account_pnl_response(
        *,
        values: dict[str, str],
        account,
    ) -> AccountRealtimePnlResponse:
        """把Redis快照或已读取的账户对象转换为统一响应。"""

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

    @staticmethod
    def _position_pnl_response(
        *,
        values: dict[str, str],
        position,
    ) -> PositionRealtimePnlResponse:
        """把Redis快照或同批持仓对象转换为统一响应。"""

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
        # PostgreSQL不保存行情事件和盯市价，缺失时明确返回None，避免伪造0价格。
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

    def get_account(
        self,
        db: Session,
        account_id: str,
        *,
        account=None,
    ) -> AccountRealtimePnlResponse:
        """
        查询账户实时盈亏。

        ``account``允许API复用账户授权阶段已经加载的对象。Redis快照缺失时，
        直接使用该对象回退，避免认证接入后再次查询同一个账户。
        """

        normalized = account_id.strip()
        try:
            values = self.pnl_store.get_account(normalized)
        except RedisError:
            values = {}
        if values:
            return self._account_pnl_response(
                values=values,
                account=None,
            )
        if account is None:
            account = self.account_repository.get_by_account_id(
                db,
                normalized,
            )
        if account is None:
            raise ResourceNotFoundError(
                "账户不存在",
                error_code="ACCOUNT_NOT_FOUND",
            )
        return self._account_pnl_response(
            values=values,
            account=account,
        )

    def get_position(
        self,
        db: Session,
        position_id: str,
        *,
        position=None,
    ) -> PositionRealtimePnlResponse:
        normalized = position_id.strip()
        try:
            values = self.pnl_store.get_position(normalized)
        except RedisError:
            values = {}
        if values:
            return self._position_pnl_response(
                values=values,
                position=None,
            )
        if position is None:
            position = self.position_repository.get_by_position_id(
                db,
                normalized,
            )
        if position is None:
            raise ResourceNotFoundError(
                "持仓不存在",
                error_code="POSITION_NOT_FOUND",
            )
        return self._position_pnl_response(
            values=values,
            position=position,
        )

    def get_account_trading_snapshot(
        self,
        db: Session,
        account_id: str,
        *,
        account=None,
    ) -> AccountTradingSnapshotResponse:
        """
        批量生成账户页面快照。

        PostgreSQL固定执行账户和持仓两次集合查询，Redis固定执行一次Pipeline；
        Redis部分或全部缺失时直接使用这批对象回退，不产生逐持仓SQL。
        """

        normalized = account_id.strip()
        if account is None:
            account = self.account_repository.get_by_account_id(
                db,
                normalized,
            )
        if account is None:
            raise ResourceNotFoundError(
                "账户不存在",
                error_code="ACCOUNT_NOT_FOUND",
            )
        positions = list(
            self.position_repository.list_by_account(
                db,
                normalized,
            )
        )
        try:
            account_values, position_values = (
                self.pnl_store.get_account_with_positions(
                    account_id=normalized,
                    position_ids=[
                        position.position_id
                        for position in positions
                    ],
                )
            )
        except RedisError:
            account_values = {}
            position_values = {}

        return AccountTradingSnapshotResponse(
            account=AccountResponse.model_validate(account),
            pnl=self._account_pnl_response(
                values=account_values,
                account=account,
            ),
            positions=[
                PositionTradingSnapshotResponse(
                    position=PositionResponse.model_validate(position),
                    pnl=self._position_pnl_response(
                        values=position_values.get(
                            position.position_id,
                            {},
                        ),
                        position=position,
                    ),
                )
                for position in positions
            ],
        )
