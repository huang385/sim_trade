import json
from dataclasses import dataclass
from typing import Mapping

from app.infrastructure.realtime_pnl_store import RealtimePnlStore
from app.services.active_position_cache import ActivePositionCache


class TradeCreatedPnlValidationError(ValueError):
    """成交事件正文不可用；重试无法自动修复该类格式错误。"""


@dataclass(frozen=True)
class TradeCreatedPnlResult:
    """成交后跨进程Dirty标记结果。"""

    action: str
    dirty_version: str | None = None
    positions_zeroed: int = 0
    snapshots_written: int = 0


class TradeCreatedPnlService:
    """
    把TRADE_CREATED转换为可靠的Redis Dirty合约标记。

    本服务不再写pnl:position或pnl:account。实时盈亏快照由唯一的
    RealtimePnlWorker统一计算和写入，避免成交Worker与行情Worker并发覆盖
    同一账户Hash。
    """

    def __init__(
        self,
        *,
        pnl_store: RealtimePnlStore,
        cache: ActivePositionCache | None = None,
        **_legacy_dependencies,
    ):
        self.cache = cache
        self.pnl_store = pnl_store

    @staticmethod
    def _parse(fields: Mapping[str, str]) -> dict | None:
        event_type = fields.get("event_type", "").strip()
        # 独立Group会看到订单流全部事件；非成交事件直接ACK。
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

    def process(
        self,
        *,
        stream_message_id: str,
        fields: Mapping[str, str],
    ) -> TradeCreatedPnlResult:
        _ = stream_message_id
        payload = self._parse(fields)
        if payload is None:
            return TradeCreatedPnlResult(action="SKIPPED")

        account_id = str(payload["account_id"]).strip()
        exchange_id = str(payload["exchange_id"]).strip().upper()
        symbol = str(payload["symbol"]).strip().upper()
        version = self.pnl_store.mark_contract_dirty(
            exchange_id=exchange_id,
            symbol=symbol,
            account_id=account_id,
        )
        # 仅让同进程测试或未来合并部署立即失效；跨进程可靠通知依赖Redis版本。
        if self.cache is not None:
            self.cache.invalidate(
                account_id=account_id,
                exchange_id=exchange_id,
                symbol=symbol,
            )
        return TradeCreatedPnlResult(
            action="DIRTY_MARKED",
            dirty_version=version,
        )
