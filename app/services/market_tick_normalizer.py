import hashlib
import json
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
    """把YMM Live Data的RQData风格Tick转换为内部MarketTick。"""

    SOURCE = "YMM_LIVE_DATA"

    @staticmethod
    def _decimal(value: Any, field_name: str) -> Decimal | None:
        """使用Decimal(str(value))转换，并拒绝NaN、Infinity和非法数字。"""

        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if isinstance(value, bool):
            raise MarketTickNormalizationError(f"{field_name}不是合法数字")
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise MarketTickNormalizationError(
                f"{field_name}不是合法数字"
            ) from exc
        if not result.is_finite():
            raise MarketTickNormalizationError(f"{field_name}不是有限数字")
        return result

    @staticmethod
    def _integer(
        value: Any,
        field_name: str,
        *,
        default: int | None = None,
    ) -> int:
        if value is None or (isinstance(value, str) and not value.strip()):
            if default is not None:
                return default
            raise MarketTickNormalizationError(f"{field_name}不能为空")
        if isinstance(value, bool):
            raise MarketTickNormalizationError(f"{field_name}必须是整数")
        try:
            decimal_value = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise MarketTickNormalizationError(
                f"{field_name}必须是整数"
            ) from exc
        if (
            not decimal_value.is_finite()
            or decimal_value != decimal_value.to_integral_value()
        ):
            raise MarketTickNormalizationError(f"{field_name}必须是整数")
        return int(decimal_value)

    @staticmethod
    def _datetime(value: Any, field_name: str) -> datetime:
        """文档中的无时区RQData datetime按Asia/Shanghai解释。"""

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
                # YMM Live Data生产回调当前使用RQData紧凑整数时间：
                # YYYYMMDDHHMMSSmmm，例如20260805095638840。
                # 文档示例中的datetime、pandas.Timestamp和ISO字符串继续兼容。
                if text.isdigit() and len(text) in {14, 17, 20}:
                    date_time_text = text[:14]
                    result = datetime.strptime(
                        date_time_text,
                        "%Y%m%d%H%M%S",
                    )
                    fraction = text[14:]
                    if fraction:
                        result = result.replace(
                            microsecond=int(fraction.ljust(6, "0"))
                        )
                else:
                    result = datetime.fromisoformat(text)
            except ValueError as exc:
                raise MarketTickNormalizationError(
                    f"{field_name}不是合法时间"
                ) from exc

        if result.tzinfo is None:
            return result.replace(tzinfo=SHANGHAI_TIMEZONE)
        return result.astimezone(SHANGHAI_TIMEZONE)

    @classmethod
    def _optional_datetime(
        cls,
        value: Any,
        field_name: str,
    ) -> datetime | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return cls._datetime(value, field_name)

    @staticmethod
    def _date(value: Any, field_name: str) -> date:
        """交易日只解析源端trading_date，绝不从本地日期或时间推算。"""

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

    @staticmethod
    def _first(values: Any) -> Any:
        if values is None or isinstance(values, (str, bytes)):
            return None
        try:
            return values[0] if len(values) else None
        except (TypeError, KeyError):
            return None

    @staticmethod
    def _identity_value(value: Any) -> Any:
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if isinstance(value, Decimal):
            return format(value, "f")
        if isinstance(value, dict):
            return {
                str(key): MarketTickNormalizer._identity_value(item)
                for key, item in sorted(value.items())
                if key not in {"local_recv_time", "server_time"}
            }
        if isinstance(value, (list, tuple)):
            return [MarketTickNormalizer._identity_value(item) for item in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)

    @classmethod
    def build_source_event_id(
        cls,
        data: dict[str, Any],
        *,
        source: str | None = None,
    ) -> str:
        """优先保留源端事件号；缺失时对稳定业务字段做SHA-256。

        SDK文档没有承诺每条Tick带事件编号，因此不能使用进程计数器。
        本地接收时间和服务器诊断时间不参与身份，重连后的同一源事件仍得到
        相同编号；Redis Stream自身的消息编号继续承担可靠消费重投身份。
        """

        explicit = data.get("source_event_id") or data.get("event_id")
        if explicit is not None and str(explicit).strip():
            return str(explicit).strip()
        canonical = json.dumps(
            cls._identity_value(data),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        digest = hashlib.sha256(
            f"{source or cls.SOURCE}|{canonical}".encode("utf-8")
        ).hexdigest()
        return (
            f"BOOTSTRAP-{digest}"
            if source == "YMM_DATA_SDK"
            else digest
        )

    @classmethod
    def _sequence_id(cls, data: dict[str, Any], source_event_id: str) -> int:
        explicit = data.get("sequence_id")
        if explicit is None:
            explicit = data.get("sequence")
        if explicit is not None and str(explicit).strip():
            return cls._integer(explicit, "sequence_id")
        # SDK未提供行情序号时，从稳定事件身份派生正整数，仅用于审计和定位。
        return int(
            hashlib.sha256(source_event_id.encode("utf-8")).hexdigest()[:15],
            16,
        )

    def normalize(
        self,
        *,
        data: dict[str, Any],
        raw: dict[str, Any],
        instrument,
        ingest_type: MarketTickIngestType = MarketTickIngestType.LIVE_CALLBACK,
        source: str = SOURCE,
    ) -> MarketTick:
        """使用Instrument补齐交易所和品种，不从合约字符串猜测业务字段。"""

        del raw  # 新SDK回调字典就是公开协议，适配层不再传递旧原始信封。
        order_book_id = normalize_code(str(data.get("order_book_id") or ""))
        trading_day = self._date(data.get("trading_date"), "trading_date")
        event_time = self._datetime(data.get("datetime"), "datetime")
        source_event_id = self.build_source_event_id(data, source=source)
        sequence_id = self._sequence_id(data, source_event_id)

        return MarketTick(
            source_event_id=source_event_id,
            source=source,
            ingest_type=ingest_type,
            order_book_id=order_book_id,
            exchange_id=instrument.exchange_id,
            symbol=instrument.symbol,
            trading_day=trading_day,
            event_time=event_time,
            local_recv_time=self._optional_datetime(
                data.get("local_recv_time"),
                "local_recv_time",
            ),
            server_time=self._optional_datetime(
                data.get("server_time"),
                "server_time",
            ),
            sequence_id=sequence_id,
            last_price=self._decimal(data.get("last"), "last"),
            pre_close=self._decimal(data.get("prev_close"), "prev_close"),
            open_price=self._decimal(data.get("open"), "open"),
            high_price=self._decimal(data.get("high"), "high"),
            low_price=self._decimal(data.get("low"), "low"),
            cumulative_volume=self._integer(
                data.get("volume"),
                "volume",
                default=0,
            ),
            cumulative_turnover=self._decimal(
                data.get("total_turnover"),
                "total_turnover",
            ),
            open_interest=self._decimal(
                data.get("open_interest"),
                "open_interest",
            ),
            bid_price_1=self._decimal(self._first(data.get("bid")), "bid[0]"),
            bid_volume_1=self._integer(
                self._first(data.get("bid_vol")),
                "bid_vol[0]",
                default=0,
            ),
            ask_price_1=self._decimal(self._first(data.get("ask")), "ask[0]"),
            ask_volume_1=self._integer(
                self._first(data.get("ask_vol")),
                "ask_vol[0]",
                default=0,
            ),
            raw_update_time=event_time.time().isoformat(),
            raw_update_millisec=event_time.microsecond // 1000,
        )
