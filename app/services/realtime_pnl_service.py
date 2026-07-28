import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping

from app.common.decimal_utils import quantize_money
from app.common.time_utils import utc_now
from app.infrastructure.realtime_pnl_store import RealtimePnlStore
from app.schemas.market_tick_schema import MarketTick
from app.schemas.pnl_schema import (
    AccountRealtimePnl,
    PositionRealtimePnl,
)
from app.services.active_position_cache import ActivePositionCache
from app.services.pnl_calculator import (
    PnlCalculator,
    PositionPnlResult,
)


RISK_QUANT = Decimal("0.00000001")


class PnlEventValidationError(ValueError):
    """行情消息结构固定错误，重试不会恢复。"""


@dataclass(frozen=True)
class RealtimePnlProcessResult:
    """单条行情实时盈亏处理统计。"""

    action: str
    positions_calculated: int = 0
    redis_snapshots_written: int = 0
    dirty_positions: int = 0


class RealtimePnlService:
    """
    根据实时行情和活动持仓内存快照计算绝对盈亏并写入Redis。

    本服务不会UPDATE或COMMIT PostgreSQL；重复Tick只会覆盖相同绝对结果，
    不累计手续费、已实现盈亏或上一轮浮盈。
    """

    def __init__(
        self,
        *,
        active_position_cache: ActivePositionCache,
        pnl_store: RealtimePnlStore,
        calculator: PnlCalculator | None = None,
    ):
        self.active_position_cache = active_position_cache
        self.pnl_store = pnl_store
        self.calculator = calculator or PnlCalculator()

    @staticmethod
    def _parse_tick(fields: Mapping[str, str]) -> MarketTick | None:
        if fields.get("event_type", "").strip() != "MARKET_TICK":
            raise PnlEventValidationError(
                "PnL消费者只支持MARKET_TICK事件"
            )
        try:
            payload = json.loads(fields.get("payload", ""))
        except (TypeError, json.JSONDecodeError) as exc:
            raise PnlEventValidationError("行情payload不是合法JSON") from exc
        if not isinstance(payload, dict):
            raise PnlEventValidationError("行情payload必须是对象")
        # REST快照和其他来源正常跳过并ACK，不进入死信。
        if (
            payload.get("source") != "YML_FEEDHUB"
            or payload.get("ingest_type") != "LIVE_CALLBACK"
        ):
            return None
        try:
            return MarketTick.model_validate(payload)
        except Exception as exc:
            raise PnlEventValidationError(
                "实时行情字段校验失败"
            ) from exc

    @staticmethod
    def _decimal_from_hash(
        values: Mapping[str, str],
        key: str,
        fallback: Decimal,
    ) -> Decimal:
        raw = values.get(key)
        return Decimal(raw) if raw not in (None, "") else fallback

    @staticmethod
    def _risk_ratio(
        used_margin: Decimal,
        equity: Decimal,
    ) -> Decimal:
        if equity <= 0:
            return Decimal("0.00000000")
        return (used_margin / equity).quantize(
            RISK_QUANT,
            rounding=ROUND_HALF_UP,
        )

    def process(
        self,
        *,
        stream_message_id: str,
        fields: Mapping[str, str],
    ) -> RealtimePnlProcessResult:
        tick = self._parse_tick(fields)
        if tick is None:
            return RealtimePnlProcessResult(action="SKIPPED")
        if tick.last_price is None or tick.last_price <= 0:
            return RealtimePnlProcessResult(action="SKIPPED")

        positions = self.active_position_cache.get_by_contract(
            tick.exchange_id,
            tick.symbol,
        )
        if not positions:
            return RealtimePnlProcessResult(action="NO_POSITION")

        calculated: dict[str, PositionPnlResult] = {}
        position_models: list[PositionRealtimePnl] = []
        updated_at = utc_now()
        for position in positions:
            result = self.calculator.calculate_position(
                mark_price=tick.last_price,
                snapshot=position,
            )
            calculated[position.position_id] = result
            position_models.append(
                PositionRealtimePnl(
                    position_id=position.position_id,
                    account_id=position.account_id,
                    exchange_id=position.exchange_id,
                    symbol=position.symbol,
                    direction=position.direction,
                    mark_price=tick.last_price,
                    cumulative_unrealized_pnl=(
                        result.cumulative_unrealized_pnl
                    ),
                    daily_position_pnl=result.daily_position_pnl,
                    event_time=tick.event_time,
                    source_event_id=tick.source_event_id,
                    updated_at=updated_at,
                )
            )

        account_models: list[AccountRealtimePnl] = []
        affected_accounts = {
            position.account_id for position in positions
        }
        for account_id in affected_accounts:
            account = self.active_position_cache.get_account(account_id)
            if account is None:
                continue
            cumulative_unrealized = Decimal("0")
            daily_position = Decimal("0")
            for position in self.active_position_cache.get_by_account(
                account_id
            ):
                current = calculated.get(position.position_id)
                if current is not None:
                    cumulative_unrealized += (
                        current.cumulative_unrealized_pnl
                    )
                    daily_position += current.daily_position_pnl
                    continue
                redis_snapshot = self.pnl_store.get_position(
                    position.position_id
                )
                cumulative_unrealized += self._decimal_from_hash(
                    redis_snapshot,
                    "cumulative_unrealized_pnl",
                    position.persisted_unrealized_pnl,
                )
                daily_position += self._decimal_from_hash(
                    redis_snapshot,
                    "daily_position_pnl",
                    position.persisted_daily_position_pnl,
                )

            cumulative_unrealized = quantize_money(
                cumulative_unrealized
            )
            daily_position = quantize_money(daily_position)
            daily_pnl = quantize_money(
                daily_position
                + account.daily_close_pnl
                - account.daily_commission
            )
            # 当前尚未日终逐日盯市，cash_balance未包含历史持仓盈亏，因此
            # equity仍使用从原始开仓价计算的累计浮盈。正式日终后应切换为
            # cash_balance + daily_position_pnl，避免历史盈亏重复计入。
            equity = quantize_money(
                account.cash_balance + cumulative_unrealized
            )
            available_cash = quantize_money(
                equity
                - account.used_margin
                - account.frozen_margin
                - account.frozen_cash
                - account.frozen_commission
            )
            account_models.append(
                AccountRealtimePnl(
                    account_id=account_id,
                    cumulative_unrealized_pnl=cumulative_unrealized,
                    daily_position_pnl=daily_position,
                    daily_close_pnl=account.daily_close_pnl,
                    daily_commission=account.daily_commission,
                    daily_pnl=daily_pnl,
                    equity=equity,
                    available_cash=available_cash,
                    risk_ratio=self._risk_ratio(
                        account.used_margin,
                        equity,
                    ),
                    updated_at=updated_at,
                )
            )

        written_positions, written_accounts = (
            self.pnl_store.write_snapshots(
                positions=position_models,
                accounts=account_models,
                dirty_version=(
                    f"{stream_message_id}:{tick.source_event_id}"
                ),
            )
        )
        return RealtimePnlProcessResult(
            action="CALCULATED",
            positions_calculated=len(position_models),
            redis_snapshots_written=(
                written_positions + written_accounts
            ),
            dirty_positions=written_positions,
        )
