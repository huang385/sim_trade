import json
from dataclasses import dataclass
from typing import Mapping

from app.infrastructure.realtime_pnl_store import RealtimePnlStore
from app.services.active_position_cache import ActivePositionCache
from app.infrastructure.risk_store import RiskStore


class TradeCreatedPnlValidationError(ValueError):
    """成交事件正文不可用；重试无法自动修复该类格式错误。"""


@dataclass(frozen=True)
class TradeCreatedPnlResult:
    """订单或成交事实提交后的跨进程Dirty标记结果。"""

    action: str
    dirty_version: str | None = None
    dirty_kind: str | None = None
    positions_zeroed: int = 0
    snapshots_written: int = 0


class TradeCreatedPnlService:
    """
    把成交、下单冻结和撤单释放事件转换为可靠的Redis分类Dirty标记。

    本服务不再写pnl:position或pnl:account。实时盈亏快照由唯一的
    RealtimePnlWorker统一计算和写入，避免成交Worker与行情Worker并发覆盖
    同一账户Hash。成交刷新合约结构，订单接受和撤单只刷新账户资金事实。
    """

    FACT_CHANGE_EVENT_TYPES = {
        "TRADE_CREATED",
        "ORDER_ACCEPTED",
        "ORDER_CANCELLED",
        "ORDER_PARTIALLY_CANCELLED",
        "ORDER_MARGIN_UPDATED",
        "POSITION_UPDATED",
        "POSITION_CLOSED",
    }

    def __init__(
        self,
        *,
        pnl_store: RealtimePnlStore,
        cache: ActivePositionCache | None = None,
        processed_ttl_seconds: int = 604800,
        risk_store: RiskStore | None = None,
        **_legacy_dependencies,
    ):
        self.cache = cache
        self.pnl_store = pnl_store
        self.processed_ttl_seconds = processed_ttl_seconds
        self.risk_store = risk_store

    @classmethod
    def _parse(
        cls,
        fields: Mapping[str, str],
    ) -> tuple[str, str, dict] | None:
        event_type = fields.get("event_type", "").strip()
        # 独立Group会看到订单流全部事件；只处理会改变持仓或账户资金基础
        # 字段的提交后事件，其他类型直接ACK。
        if event_type not in cls.FACT_CHANGE_EVENT_TYPES:
            return None
        try:
            payload = json.loads(fields.get("payload", ""))
        except (TypeError, json.JSONDecodeError) as exc:
            raise TradeCreatedPnlValidationError(
                "PnL事实事件payload不是合法JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise TradeCreatedPnlValidationError(
                "PnL事实事件payload必须是对象"
            )
        # 股票和可转债拥有独立的账户、持仓估值事实链路，不能进入衍生品的
        # Dirty/Risk 管道；否则两个估值写者会并发覆盖同一账户字段。
        if str(payload.get("instrument_type") or "").upper() in {
            "STOCK",
            "CONVERTIBLE_BOND",
        }:
            return None
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
        required = ("account_id", "exchange_id", "order_book_id")
        if any(not str(payload.get(name, "")).strip() for name in required):
            raise TradeCreatedPnlValidationError(
                "账户事实事件缺少实时盈亏刷新字段"
            )
        if not event_id:
            raise TradeCreatedPnlValidationError(
                "账户事实事件缺少event_id"
            )
        if (
            event_type in {"POSITION_UPDATED", "POSITION_CLOSED"}
            and payload.get("fact_reason") != "OPTION_MARGIN_ADJUSTMENT"
        ):
            # 普通成交已经由同事务的TRADE_CREATED触发结构Dirty；只处理
            # 期权保证金重估专属持仓事实，避免一笔成交重复刷新两次。
            return None
        return event_type, event_id, payload

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
        event_type, event_id, payload = parsed

        account_id = str(payload["account_id"]).strip()
        exchange_id = str(payload.get("exchange_id") or "").strip().upper()
        order_book_id = str(payload.get("order_book_id") or "").strip().upper()
        if event_type in {
            "TRADE_CREATED",
            "POSITION_UPDATED",
            "POSITION_CLOSED",
        }:
            version = self.pnl_store.mark_contract_dirty_once(
                event_id=event_id,
                exchange_id=exchange_id,
                order_book_id=order_book_id,
                account_id=account_id,
                processed_ttl_seconds=self.processed_ttl_seconds,
            )
            dirty_kind = "CONTRACT_STRUCTURE"
        else:
            # 订单接受和撤单只改变账户冻结资金等基础字段，不改变Position
            # 结构，因此不能递增全局持仓缓存版本或触发全量持仓查询。
            version = self.pnl_store.mark_account_fact_dirty_once(
                event_id=event_id,
                account_id=account_id,
                processed_ttl_seconds=self.processed_ttl_seconds,
            )
            dirty_kind = "ACCOUNT_FACT"
        if version is None:
            return TradeCreatedPnlResult(action="DUPLICATE")
        if self.risk_store is not None:
            # 与PnL使用不同幂等键域；任一步Redis失败都会让Stream消息保留Pending重试。
            self.risk_store.mark_dirty_once(
                account_id=account_id,
                event_id=event_id,
                ttl_seconds=self.processed_ttl_seconds,
            )
        # 仅让同进程测试或未来合并部署立即失效；跨进程可靠通知依赖Redis版本。
        if self.cache is not None:
            if dirty_kind == "CONTRACT_STRUCTURE":
                self.cache.invalidate(
                    account_id=account_id,
                    exchange_id=exchange_id,
                    order_book_id=order_book_id,
                )
            else:
                self.cache.invalidate(account_id=account_id)
        return TradeCreatedPnlResult(
            action="DIRTY_MARKED",
            dirty_version=version,
            dirty_kind=dirty_kind,
        )
