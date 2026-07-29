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
    把成交、下单冻结和撤单释放事件转换为可靠的Redis Dirty合约标记。

    本服务不再写pnl:position或pnl:account。实时盈亏快照由唯一的
    RealtimePnlWorker统一计算和写入，避免成交Worker与行情Worker并发覆盖
    同一账户Hash。
    """

    FACT_CHANGE_EVENT_TYPES = {
        "TRADE_CREATED",
        "ORDER_ACCEPTED",
        "ORDER_CANCELLED",
        "ORDER_PARTIALLY_CANCELLED",
    }

    def __init__(
        self,
        *,
        pnl_store: RealtimePnlStore,
        cache: ActivePositionCache | None = None,
        processed_ttl_seconds: int = 604800,
        **_legacy_dependencies,
    ):
        self.cache = cache
        self.pnl_store = pnl_store
        self.processed_ttl_seconds = processed_ttl_seconds

    @classmethod
    def _parse(
        cls,
        fields: Mapping[str, str],
    ) -> tuple[str, dict] | None:
        event_type = fields.get("event_type", "").strip()
        # 独立Group会看到订单流全部事件；只处理会改变持仓或账户资金基础
        # 字段的提交后事件，其他类型直接ACK。
        if event_type not in cls.FACT_CHANGE_EVENT_TYPES:
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
        event_id = str(
            fields.get("event_id")
            or payload.get("event_id")
            or ""
        ).strip()
        if (
            payload.get("event_id")
            and str(payload["event_id"]).strip() != event_id
        ):
            raise TradeCreatedPnlValidationError(
                "账户事实事件event_id与payload不一致"
            )
        required = ("account_id", "exchange_id", "symbol")
        if any(not str(payload.get(name, "")).strip() for name in required):
            raise TradeCreatedPnlValidationError(
                "账户事实事件缺少实时盈亏刷新字段"
            )
        if not event_id:
            raise TradeCreatedPnlValidationError(
                "账户事实事件缺少event_id"
            )
        return event_id, payload

    def process(
        self,
        *,
        stream_message_id: str,
        fields: Mapping[str, str],
    ) -> TradeCreatedPnlResult:
        _ = stream_message_id
        parsed = self._parse(fields)
        if parsed is None:
            return TradeCreatedPnlResult(action="SKIPPED")
        event_id, payload = parsed

        account_id = str(payload["account_id"]).strip()
        exchange_id = str(payload["exchange_id"]).strip().upper()
        symbol = str(payload["symbol"]).strip().upper()
        version = self.pnl_store.mark_contract_dirty_once(
            event_id=event_id,
            exchange_id=exchange_id,
            symbol=symbol,
            account_id=account_id,
            processed_ttl_seconds=self.processed_ttl_seconds,
        )
        if version is None:
            return TradeCreatedPnlResult(action="DUPLICATE")
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
