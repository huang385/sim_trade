import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping

from app.common.decimal_utils import quantize_money
from app.common.time_utils import utc_now
from app.infrastructure.market_data.market_tick_store import MarketTickStore
from app.infrastructure.realtime_pnl_store import RealtimePnlStore
from app.repositories.account_repository import AccountRepository
from app.repositories.position_repository import PositionRepository
from app.schemas.market_tick_schema import MarketTick
from app.schemas.pnl_schema import (
    AccountRealtimePnl,
    PositionRealtimePnl,
)
from app.services.active_position_cache import ActivePositionCache
from app.services.realtime_pnl_service import RealtimePnlService


RISK_QUANT = Decimal("0.00000001")


class TradeCreatedPnlValidationError(ValueError):
    """成交事件正文不可用；重试无法自动修复该类格式错误。"""


@dataclass(frozen=True)
class TradeCreatedPnlResult:
    """成交后实时盈亏刷新结果。"""

    action: str
    positions_zeroed: int = 0
    snapshots_written: int = 0


class TradeCreatedPnlService:
    """
    在TRADE_CREATED已发布后刷新持仓缓存和Redis实时盈亏。

    该服务运行在数据库成交事务之外，因此Redis故障不会回滚已经提交的Trade；
    重放同一事件时仍写绝对值，不会重复累计盈亏或手续费。
    """

    def __init__(
        self,
        *,
        session_factory,
        cache: ActivePositionCache,
        pnl_store: RealtimePnlStore,
        market_tick_store: MarketTickStore,
        realtime_service: RealtimePnlService,
        account_repository: AccountRepository | None = None,
        position_repository: PositionRepository | None = None,
    ):
        self.session_factory = session_factory
        self.cache = cache
        self.pnl_store = pnl_store
        self.market_tick_store = market_tick_store
        self.realtime_service = realtime_service
        self.account_repository = (
            account_repository or AccountRepository()
        )
        self.position_repository = (
            position_repository or PositionRepository()
        )

    @staticmethod
    def _parse(fields: Mapping[str, str]) -> dict | None:
        event_type = fields.get("event_type", "").strip()
        # 独立Group会看到订单流中的全部事件；非成交事件直接ACK，不进入死信。
        if event_type != "TRADE_CREATED":
            return None
        try:
            payload = json.loads(fields.get("payload", ""))
        except (TypeError, json.JSONDecodeError) as exc:
            raise TradeCreatedPnlValidationError(
                "TRADE_CREATED payload不是合法JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise TradeCreatedPnlValidationError(
                "TRADE_CREATED payload必须是对象"
            )
        required = (
            "event_id",
            "account_id",
            "exchange_id",
            "symbol",
            "trade_time",
        )
        if any(not str(payload.get(name, "")).strip() for name in required):
            raise TradeCreatedPnlValidationError(
                "TRADE_CREATED缺少实时盈亏刷新字段"
            )
        return payload

    @staticmethod
    def _risk(used_margin: Decimal, equity: Decimal) -> Decimal:
        if equity <= 0:
            return Decimal("0.00000000")
        return (used_margin / equity).quantize(
            RISK_QUANT,
            rounding=ROUND_HALF_UP,
        )

    def _write_account_from_current_positions(
        self,
        *,
        account,
        updated_at: datetime,
    ) -> int:
        """从当前活动持仓绝对快照重新汇总账户，排除已全部平掉的持仓。"""

        cumulative = Decimal("0")
        daily_position = Decimal("0")
        for position in self.cache.get_by_account(account.account_id):
            values = self.pnl_store.get_position(position.position_id)
            cumulative += Decimal(
                values.get(
                    "cumulative_unrealized_pnl",
                    str(position.persisted_unrealized_pnl),
                )
            )
            daily_position += Decimal(
                values.get(
                    "daily_position_pnl",
                    str(position.persisted_daily_position_pnl),
                )
            )
        cumulative = quantize_money(cumulative)
        daily_position = quantize_money(daily_position)
        daily_pnl = quantize_money(
            daily_position
            + account.daily_close_pnl
            - account.daily_commission
        )
        equity = quantize_money(account.cash_balance + cumulative)
        available = quantize_money(
            equity
            - account.used_margin
            - account.frozen_margin
            - account.frozen_cash
            - account.frozen_commission
        )
        _positions, accounts = self.pnl_store.write_snapshots(
            positions=[],
            accounts=[
                AccountRealtimePnl(
                    account_id=account.account_id,
                    cumulative_unrealized_pnl=cumulative,
                    daily_position_pnl=daily_position,
                    daily_close_pnl=account.daily_close_pnl,
                    daily_commission=account.daily_commission,
                    daily_pnl=daily_pnl,
                    equity=equity,
                    available_cash=available,
                    risk_ratio=self._risk(
                        account.used_margin,
                        equity,
                    ),
                    updated_at=updated_at,
                )
            ],
            dirty_version=f"trade-account:{updated_at.isoformat()}",
        )
        return accounts

    def process(
        self,
        *,
        stream_message_id: str,
        fields: Mapping[str, str],
    ) -> TradeCreatedPnlResult:
        payload = self._parse(fields)
        if payload is None:
            return TradeCreatedPnlResult(action="SKIPPED")

        account_id = str(payload["account_id"]).strip()
        exchange_id = str(payload["exchange_id"]).strip().upper()
        symbol = str(payload["symbol"]).strip().upper()
        tracked_position_ids = self.pnl_store.list_contract_position_ids(
            exchange_id,
            symbol,
        )
        # 独立进程中的行情PnL Worker会在下一条Tick读取此版本并立即放弃旧缓存。
        self.pnl_store.bump_position_cache_version()
        self.cache.invalidate(
            account_id=account_id,
            exchange_id=exchange_id,
            symbol=symbol,
        )

        latest = self.market_tick_store.get_latest(exchange_id, symbol)
        snapshots_written = 0
        event_time = datetime.fromisoformat(str(payload["trade_time"]))
        mark_price = Decimal(str(payload.get("trade_price", "0")))
        if latest:
            try:
                tick = MarketTick.model_validate(latest)
            except Exception as exc:
                raise TradeCreatedPnlValidationError(
                    "Redis最新行情快照格式不合法"
                ) from exc
            event_time = tick.event_time
            if tick.last_price is not None:
                mark_price = tick.last_price
            result = self.realtime_service.process(
                stream_message_id=stream_message_id,
                fields={
                    "event_type": "MARKET_TICK",
                    "payload": MarketTickStore.tick_to_payload(tick),
                },
            )
            snapshots_written += result.redis_snapshots_written

        with self.session_factory() as db:
            account = self.account_repository.get_by_account_id(
                db,
                account_id,
            )
            positions = (
                self.position_repository.list_by_account_contract(
                    db,
                    account_id=account_id,
                    exchange_id=exchange_id,
                    symbol=symbol,
                )
            )
            # ORM对象仅在Session内部读取；写入Redis前全部转换为Pydantic标量对象。
            closed = [
                item
                for item in positions
                if item.total_volume <= 0
                and item.position_id in tracked_position_ids
            ]
            if account is None:
                raise TradeCreatedPnlValidationError(
                    "成交事件对应账户不存在"
                )
            account_snapshot = account

        zero_models = [
            PositionRealtimePnl(
                position_id=item.position_id,
                account_id=item.account_id,
                exchange_id=item.exchange_id,
                symbol=item.symbol,
                direction=item.direction,
                mark_price=mark_price,
                cumulative_unrealized_pnl=Decimal("0.000000"),
                daily_position_pnl=Decimal("0.000000"),
                event_time=event_time,
                source_event_id=str(payload["event_id"]),
                updated_at=utc_now(),
            )
            for item in closed
        ]
        if zero_models:
            written, _accounts = self.pnl_store.write_snapshots(
                positions=zero_models,
                accounts=[],
                dirty_version=f"trade:{payload['event_id']}",
            )
            snapshots_written += written
            for item in zero_models:
                # 保留pnl:position:{id}=0供历史持仓查询，但不再参与活动合约汇总。
                self.pnl_store.remove_contract_position(
                    exchange_id=item.exchange_id,
                    symbol=item.symbol,
                    account_id=item.account_id,
                    position_id=item.position_id,
                )

        # 即使全部平仓后已经没有活动Position，也必须立刻把账户旧浮盈清零。
        snapshots_written += self._write_account_from_current_positions(
            account=account_snapshot,
            updated_at=utc_now(),
        )
        return TradeCreatedPnlResult(
            action="REFRESHED",
            positions_zeroed=len(zero_models),
            snapshots_written=snapshots_written,
        )
