import hashlib
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from app.common.code_utils import normalize_code
from app.schemas.market_tick_schema import MarketTick, MarketTickIngestType


SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")


class MarketTickNormalizationError(ValueError):
    """行情字段无法转换为内部统一类型。"""


class MarketTickNormalizer:
    """把优美利 FeedHub 原始字段转换为统一的 MarketTick。"""

    SOURCE = "YML_FEEDHUB"

    @staticmethod
    def _decimal(value: Any, field_name: str) -> Decimal | None:
        """使用 Decimal(str(value)) 转换，并拒绝 NaN、Infinity 和非法数字。"""

        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if isinstance(value, bool):
            raise MarketTickNormalizationError(f"{field_name}不是合法数字")
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise MarketTickNormalizationError(f"{field_name}不是合法数字") from exc
        if not result.is_finite():
            raise MarketTickNormalizationError(f"{field_name}不是有限数字")
        return result

    @staticmethod
    def _integer(value: Any, field_name: str, *, default: int | None = None) -> int:
        if value is None or (isinstance(value, str) and not value.strip()):
            if default is not None:
                return default
            raise MarketTickNormalizationError(f"{field_name}不能为空")
        if isinstance(value, bool):
            raise MarketTickNormalizationError(f"{field_name}必须是整数")
        try:
            decimal_value = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise MarketTickNormalizationError(f"{field_name}必须是整数") from exc
        if (
            not decimal_value.is_finite()
            or decimal_value != decimal_value.to_integral_value()
        ):
            raise MarketTickNormalizationError(f"{field_name}必须是整数")
        return int(decimal_value)

    @staticmethod
    def _datetime(value: Any, field_name: str) -> datetime:
        """解析时间，并把 FeedHub 无时区时间按 Asia/Shanghai 解释。"""

        if value is None or (isinstance(value, str) and not value.strip()):
            raise MarketTickNormalizationError(f"{field_name}不能为空")
        to_python = getattr(value, "to_pydatetime", None)
        if callable(to_python):
            value = to_python()

        if isinstance(value, datetime):
            result = value
        elif isinstance(value, date):
            result = datetime.combine(value, datetime.min.time())
        else:
            text = str(value).strip().replace("Z", "+00:00")
            try:
                result = datetime.fromisoformat(text)
            except ValueError as exc:
                raise MarketTickNormalizationError(
                    f"{field_name}不是合法时间"
                ) from exc

        # 统一成 aware datetime，后续可安全地与 UTC 或其他 aware 时间比较。
        if result.tzinfo is None:
            return result.replace(tzinfo=SHANGHAI_TIMEZONE)
        return result.astimezone(SHANGHAI_TIMEZONE)

    @classmethod
    def _optional_datetime(cls, value: Any, field_name: str) -> datetime | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return cls._datetime(value, field_name)

    @staticmethod
    def _date(value: Any, field_name: str) -> date:
        """交易日必须由行情源提供，本方法只解析，不从 event_time 推导。"""

        if value is None or (isinstance(value, str) and not value.strip()):
            raise MarketTickNormalizationError(f"{field_name}不能为空")
        to_python = getattr(value, "to_pydatetime", None)
        if callable(to_python):
            value = to_python()
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        text = str(value).strip()
        try:
            if text.isdigit() and len(text) == 8:
                return datetime.strptime(text, "%Y%m%d").date()
            return date.fromisoformat(text[:10])
        except ValueError as exc:
            raise MarketTickNormalizationError(
                f"{field_name}不是合法日期"
            ) from exc

    @classmethod
    def build_source_event_id(
        cls,
        *,
        exchange_id: str,
        order_book_id: str,
        trading_day: date,
        event_time: datetime,
        sequence_id: int,
    ) -> str:
        """按稳定字段生成跨进程、跨重连一致的 SHA-256 事件编号。"""

        identity = "|".join(
            (
                cls.SOURCE,
                exchange_id,
                order_book_id,
                trading_day.isoformat(),
                event_time.isoformat(timespec="microseconds"),
                str(sequence_id),
            )
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def normalize(
        self,
        *,
        data: dict[str, Any],
        raw: dict[str, Any],
        instrument,
        ingest_type: MarketTickIngestType = MarketTickIngestType.LIVE_CALLBACK,
    ) -> MarketTick:
        """使用已查询到的 Instrument 补齐 symbol，禁止从合约代码猜测品种。"""

        order_book_id = normalize_code(str(data.get("code") or ""))
        exchange_id = normalize_code(str(data.get("exchange") or ""))
        trading_day = self._date(data.get("trading_day"), "trading_day")
        event_time = self._datetime(data.get("event_time"), "event_time")
        sequence_id = self._integer(data.get("sequence_id"), "sequence_id")
        source_event_id = self.build_source_event_id(
            exchange_id=exchange_id,
            order_book_id=order_book_id,
            trading_day=trading_day,
            event_time=event_time,
            sequence_id=sequence_id,
        )

        return MarketTick(
            source_event_id=source_event_id,
            ingest_type=ingest_type,
            order_book_id=order_book_id,
            exchange_id=exchange_id,
            symbol=instrument.symbol,
            trading_day=trading_day,
            event_time=event_time,
            local_recv_time=self._optional_datetime(
                data.get("local_recv_time"), "local_recv_time"
            ),
            server_time=self._optional_datetime(raw.get("server_time"), "server_time"),
            sequence_id=sequence_id,
            last_price=self._decimal(data.get("last_price"), "last_price"),
            pre_close=self._decimal(data.get("pre_close"), "pre_close"),
            open_price=self._decimal(data.get("open"), "open"),
            high_price=self._decimal(data.get("high"), "high"),
            low_price=self._decimal(data.get("low"), "low"),
            cumulative_volume=self._integer(
                data.get("cum_volume"), "cum_volume", default=0
            ),
            cumulative_turnover=self._decimal(
                data.get("cum_turnover"), "cum_turnover"
            ),
            open_interest=self._decimal(data.get("open_interest"), "open_interest"),
            bid_price_1=self._decimal(data.get("bid_price_1"), "bid_price_1"),
            bid_volume_1=self._integer(
                data.get("bid_volume_1"), "bid_volume_1", default=0
            ),
            ask_price_1=self._decimal(data.get("ask_price_1"), "ask_price_1"),
            ask_volume_1=self._integer(
                data.get("ask_volume_1"), "ask_volume_1", default=0
            ),
            raw_update_time=(
                str(data.get("raw_update_time")).strip()
                if data.get("raw_update_time") not in (None, "")
                else None
            ),
            raw_update_millisec=(
                self._integer(data.get("raw_update_millisec"), "raw_update_millisec")
                if data.get("raw_update_millisec") not in (None, "")
                else None
            ),
        )
