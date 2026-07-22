from __future__ import annotations

import asyncio
import datetime as dt
import json
import ssl
import threading
import time
from typing import Any, AsyncIterator
from urllib.parse import urlencode
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener, urlopen

import pandas as pd

__version__ = "1.0.0"
__updated_at__ = "2026-06-10"

print(
    "\n".join(
        [
            "欢迎使用优美利行情源",
            f"Version: {__version__}",
            f"Updated at: {__updated_at__}",
            "使用方法请查看 YML_FeedHub_SDK_for_users.md。请勿随意修改此py文件。",
            "如遇 bug，或有其他特殊需求，请联系我",
        ]
    )
)


def timestamped_print(*args: object, **kwargs: object) -> None:
    flush = bool(kwargs.pop("flush", False))
    sep = str(kwargs.pop("sep", " "))
    end = str(kwargs.pop("end", "\n"))
    file = kwargs.pop("file", None)
    prefix = dt.datetime.now().isoformat(sep=" ", timespec="milliseconds")
    print(f"[{prefix}]", *args, sep=sep, end=end, file=file, flush=flush)


_TIMESTAMP_FIELDS = {
    "time",
    "event_time",
    "local_recv_time",
    "updated_at",
    "server_time",
    "listed_date",
    "expire_date",
    "last_trade_date",
    "date",
    "trading_date",
    "true_date",
    "update_time",
    "trading_day",
    "started_at",
    "stopped_at",
}


class RemoteWebSocketSubscription:
    """Handle returned by RemoteMarketDataClient.start_quote_callbacks()."""

    def __init__(self, thread: threading.Thread, stop_event: threading.Event) -> None:
        self._thread = thread
        self._stop_event = stop_event

    def stop(self) -> None:
        """Request the background WebSocket callback loop to stop."""
        self._stop_event.set()

    def join(self, timeout: float | None = None) -> None:
        """Wait for the background callback thread to exit."""
        self._thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        """Return whether the background callback thread is still running."""
        return self._thread.is_alive()


class RemoteMarketDataClient:
    """Small cross-project client for the market-data HTTP and WebSocket API.

    Naming rule:
    - Contract identifiers are always named code/codes.
    - REST and WebSocket payloads returned by the server also use code.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:54111",
        *,
        timeout: float = 3.0,
        api_user: str = "",
        api_token: str = "",
        verify_ssl: bool = True,
        use_env_proxy: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.api_user = str(api_user or "").strip()
        self.api_token = str(api_token or "").strip()
        self.use_env_proxy = use_env_proxy
        self._ssl_context: ssl.SSLContext | None = None
        if not verify_ssl:
            self._ssl_context = ssl.create_default_context()
            self._ssl_context.check_hostname = False
            self._ssl_context.verify_mode = ssl.CERT_NONE
        handlers = []
        if not use_env_proxy:
            handlers.append(ProxyHandler({}))
        if self._ssl_context is not None:
            handlers.append(HTTPSHandler(context=self._ssl_context))
        self._opener = build_opener(*handlers) if handlers else None

    def _extra_headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self.api_user:
            h["X-Api-User"] = self.api_user
        if self.api_token:
            h["X-Api-Token"] = self.api_token
        return h

    @staticmethod
    def _require_code(code: str | None, legacy: dict[str, Any]) -> str:
        if legacy:
            unexpected = ", ".join(sorted(legacy))
            raise TypeError(f"unexpected keyword argument(s): {unexpected}")
        value = str(code or "").strip()
        if not value:
            raise ValueError("code is required")
        return value

    @staticmethod
    def _require_codes(codes: list[str] | str, legacy: dict[str, Any]) -> list[str]:
        if legacy:
            unexpected = ", ".join(sorted(legacy))
            raise TypeError(f"unexpected keyword argument(s): {unexpected}")
        if isinstance(codes, str):
            raw_values = [item.strip() for item in codes.split(",")]
        else:
            raw_values = list(codes or [])
        result = [str(item).strip() for item in raw_values if str(item).strip()]
        if not result:
            raise ValueError("codes is required")
        return result

    def health(self, *, expect_df: bool = True) -> pd.DataFrame | dict[str, Any]:
        """Return GET /health."""
        payload = self._get("/health")
        return self._dict_result(payload, expect_df=expect_df)

    def get_status(self, *, expect_df: bool = True) -> pd.DataFrame | dict[str, Any]:
        """Return GET /api/v1/status."""
        payload = self._get("/api/v1/status")
        return self._dict_result(payload, expect_df=expect_df)

    def get_runtime(self, *, expect_df: bool = True) -> pd.DataFrame | dict[str, Any]:
        """Compatibility alias for get_status()."""
        return self.get_status(expect_df=expect_df)

    def get_latest_tick(
        self,
        code: str = "",
        *,
        expect_df: bool = True,
        **legacy: object,
    ) -> pd.DataFrame | dict[str, Any] | None:
        """Query the latest tick for one contract code.

        By default returns a one-row DataFrame.  With expect_df=False returns
        the inner ``tick`` payload for the first server item, or None.
        """
        code = self._require_code(code, legacy)
        payload = self._get("/api/v1/ticks/latest", {"code": code})
        if expect_df:
            return pd.DataFrame(self._convert_time_fields(self._flatten_tick_items([code], payload)))
        ticks = payload.get("ticks", [])
        if ticks:
            first = ticks[0]
            if isinstance(first, dict):
                tick = first.get("tick")
                return self._convert_time_fields(dict(tick)) if isinstance(tick, dict) else None
        return None

    def get_latest_ticks(
        self,
        codes: list[str] | str,
        *,
        expect_df: bool = True,
        **legacy: object,
    ) -> pd.DataFrame | dict[str, dict[str, Any] | None]:
        """Query latest ticks for multiple contract codes.

        By default returns a DataFrame with one row per requested code.
        With expect_df=False returns {"AG2606": tick_dict_or_none}.
        """
        codes = self._require_codes(codes, legacy)
        payload = self._get("/api/v1/ticks/latest", {"codes": ",".join(codes)})
        if expect_df:
            return pd.DataFrame(self._convert_time_fields(self._flatten_tick_items(codes, payload)))
        rows: dict[str, dict[str, Any] | None] = {code: None for code in codes}
        for index, item in enumerate(payload.get("ticks", [])):
            if not isinstance(item, dict):
                continue
            tick = item.get("tick")
            item_code = str(item.get("code") or "").strip()
            input_code = str(item.get("input_code") or "").strip()
            if not item_code and isinstance(tick, dict):
                item_code = str(tick.get("code") or "").strip()
            if input_code:
                item_code = input_code
            elif not item_code and index < len(codes):
                item_code = codes[index]
            if not item_code:
                continue
            rows[item_code] = self._convert_time_fields(dict(tick)) if isinstance(tick, dict) else None
        return rows

    def get_bar_window(
        self,
        code: str = "",
        *,
        freq: str = "1m",
        limit: int | None = 200,
        start: str | dt.date | dt.datetime | pd.Timestamp | None = None,
        end: str | dt.date | dt.datetime | pd.Timestamp | None = None,
        include_live: bool = True,
        expect_df: bool = True,
        **legacy: object,
    ) -> pd.DataFrame | list[dict[str, Any]]:
        """Query a K-line window for one contract code."""
        code_value = self._require_code(code, legacy)
        rows = self._get_bar_window_one(
            code_value,
            freq=freq,
            limit=limit,
            start=start,
            end=end,
            include_live=include_live,
        )
        return self._rows_result(rows, expect_df=expect_df)

    def get_bar_windows(
        self,
        codes: list[str] | str,
        *,
        freq: str = "1m",
        limit: int | None = 200,
        start: str | dt.date | dt.datetime | pd.Timestamp | None = None,
        end: str | dt.date | dt.datetime | pd.Timestamp | None = None,
        include_live: bool = True,
        expect_df: bool = True,
        **legacy: object,
    ) -> dict[str, pd.DataFrame] | dict[str, list[dict[str, Any]]]:
        """Query K-line windows for multiple contract codes."""
        code_values = self._require_codes(codes, legacy)
        rows_by_code = {
            item: self._get_bar_window_one(
                item,
                freq=freq,
                limit=limit,
                start=start,
                end=end,
                include_live=include_live,
            )
            for item in code_values
        }
        if not expect_df:
            return rows_by_code
        return {code: pd.DataFrame(self._convert_time_fields(rows)) for code, rows in rows_by_code.items()}

    def validate_contracts(
        self,
        codes: list[str] | str,
        *,
        expect_df: bool = True,
        **legacy: object,
    ) -> pd.DataFrame | dict[str, dict[str, Any]]:
        """Check whether contracts exist and whether they are marked is_live."""
        code_values = self._require_codes(codes, legacy)
        payload = self._get("/api/v1/contracts/validate", {"codes": ",".join(code_values)})
        rows = self._convert_time_fields([dict(row) for row in payload.get("contracts", []) if isinstance(row, dict)])
        if expect_df:
            return pd.DataFrame(rows)
        result: dict[str, dict[str, Any]] = {}
        for index, row in enumerate(rows):
            key = str(row.get("input_code") or "").strip()
            if not key and index < len(code_values):
                key = code_values[index]
            if key:
                result[key] = row
        for code_value in code_values:
            result.setdefault(code_value, {})
        return result

    def _get_bar_window_one(
        self,
        code: str,
        *,
        freq: str,
        limit: int | None,
        start: str | dt.date | dt.datetime | pd.Timestamp | None,
        end: str | dt.date | dt.datetime | pd.Timestamp | None,
        include_live: bool,
    ) -> list[dict[str, Any]]:
        start_value = self._normalize_datetime_arg(start, name="start")
        end_value = self._normalize_datetime_arg(end, name="end")
        self._validate_bar_window_args(limit=limit, start=start_value)
        if limit is not None:
            return self._fetch_bar_window_page(
                code,
                freq=freq,
                limit=int(limit),
                start=start_value,
                end=end_value,
                include_live=include_live,
            )
        return self._fetch_bar_window_all(
            code,
            freq=freq,
            start=str(start_value),
            end=end_value,
            include_live=include_live,
        )

    @staticmethod
    def _normalize_datetime_arg(value: str | dt.date | dt.datetime | pd.Timestamp | None, *, name: str) -> str | None:
        if value is None:
            return None
        to_pydatetime = getattr(value, "to_pydatetime", None)
        if callable(to_pydatetime):
            value = to_pydatetime()
        if isinstance(value, dt.datetime):
            return RemoteMarketDataClient._format_datetime_arg(value)
        if isinstance(value, dt.date):
            return RemoteMarketDataClient._format_datetime_arg(dt.datetime.combine(value, dt.time()))

        text = str(value).strip()
        if not text:
            return None
        if text.lower() == "now":
            raise ValueError(f"{name}=None means now; explicit {name}='now' is not supported by the SDK")

        parsed = RemoteMarketDataClient._parse_datetime_text(text)
        if parsed is None:
            raise ValueError(f"invalid {name} datetime: {text}")
        return RemoteMarketDataClient._format_datetime_arg(parsed)

    @staticmethod
    def _parse_datetime_text(text: str) -> dt.datetime | None:
        if text.isdigit():
            digit_formats = {
                14: "%Y%m%d%H%M%S",
                12: "%Y%m%d%H%M",
                8: "%Y%m%d",
            }
            fmt = digit_formats.get(len(text))
            if fmt:
                try:
                    return dt.datetime.strptime(text, fmt)
                except ValueError:
                    return None

        try:
            parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
        except ValueError:
            pass

        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
            "%Y%m%d %H%M%S",
            "%Y%m%d %H%M",
            "%Y%m%d %H:%M:%S",
            "%Y%m%d %H:%M",
            "%Y-%m-%d",
            "%Y/%m/%d",
        ):
            try:
                return dt.datetime.strptime(text, fmt)
            except ValueError:
                continue
        return None

    @staticmethod
    def _format_datetime_arg(value: dt.datetime) -> str:
        if value.tzinfo is not None:
            value = value.replace(tzinfo=None)
        timespec = "milliseconds" if value.microsecond else "seconds"
        return value.isoformat(timespec=timespec)

    @staticmethod
    def _validate_bar_window_args(*, limit: int | None, start: str | None) -> None:
        if limit is None and not str(start or "").strip():
            raise ValueError("start is required when limit is None")
        if limit is not None and int(limit) <= 0:
            raise ValueError("limit must be a positive int or None")

    @staticmethod
    def _csv_arg(value: list[Any] | tuple[Any, ...] | str | int | None) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, int):
            return str(value)
        return ",".join(str(item).strip() for item in value if str(item).strip())

    @staticmethod
    def _text_keys(value: list[Any] | tuple[Any, ...] | str) -> list[str]:
        if isinstance(value, str):
            rows = [item.strip() for item in value.split(",") if item.strip()]
        else:
            rows = [str(item).strip() for item in value if str(item).strip()]
        if not rows:
            raise ValueError("value is required")
        return rows

    @staticmethod
    def _clean_params(params: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in params.items() if value not in (None, "")}

    @classmethod
    def _rows_result(cls, rows: list[dict[str, Any]], *, expect_df: bool) -> pd.DataFrame | list[dict[str, Any]]:
        rows = cls._convert_time_fields(rows)
        if expect_df:
            return pd.DataFrame(rows)
        return rows

    @classmethod
    def _dict_result(cls, row: dict[str, Any], *, expect_df: bool) -> pd.DataFrame | dict[str, Any]:
        row = cls._convert_time_fields(dict(row))
        if expect_df:
            return pd.DataFrame([dict(row)])
        return row

    @classmethod
    def _convert_time_fields(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [cls._convert_time_fields(item) for item in value]
        if isinstance(value, tuple):
            return tuple(cls._convert_time_fields(item) for item in value)
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for key, item in value.items():
                if key in _TIMESTAMP_FIELDS:
                    out[key] = cls._to_timestamp_or_original(item)
                else:
                    out[key] = cls._convert_time_fields(item)
            return out
        return value

    @staticmethod
    def _to_timestamp_or_original(value: Any) -> Any:
        if value in (None, ""):
            return value
        if isinstance(value, pd.Timestamp):
            return value
        if isinstance(value, dt.datetime):
            return pd.Timestamp(value)
        if isinstance(value, dt.date):
            return pd.Timestamp(dt.datetime.combine(value, dt.time()))
        text = str(value).strip()
        if not text:
            return value
        try:
            if text.isdigit() and len(text) == 8:
                return pd.to_datetime(text, format="%Y%m%d")
            # tick update_time: "20260606 14:30:45.123"
            if len(text) >= 17 and text[8:9] == " " and text[:8].isdigit():
                fmt = "%Y%m%d %H:%M:%S.%f" if "." in text else "%Y%m%d %H:%M:%S"
                return pd.to_datetime(text, format=fmt)
            return pd.Timestamp(text)
        except Exception:
            return value

    @staticmethod
    def _flatten_tick_items(codes: list[str], payload: dict[str, Any]) -> list[dict[str, Any]]:
        items = payload.get("ticks", [])
        by_code: dict[str, dict[str, Any]] = {}
        ordered: list[dict[str, Any]] = []
        for index, item in enumerate(items if isinstance(items, list) else []):
            if not isinstance(item, dict):
                continue
            tick = item.get("tick")
            row = {
                "code": str(item.get("code") or "").strip(),
                "input_code": str(item.get("input_code") or "").strip(),
                "exchange": item.get("exchange", ""),
                "quote_status": item.get("quote_status", "unknown"),
                "contract_exists": item.get("contract_exists"),
                "is_live": item.get("is_live"),
            }
            if isinstance(tick, dict):
                row.update(dict(tick))
                row.setdefault("quote_status", item.get("quote_status", "ok"))
            if not row.get("code") and index < len(codes):
                row["code"] = codes[index]
            input_code = str(item.get("input_code") or "").strip()
            code = input_code or str(row.get("code") or "").strip()
            if code:
                by_code[code] = row
            ordered.append(row)

        result: list[dict[str, Any]] = []
        used: set[int] = set()
        for index, code in enumerate(codes):
            row = by_code.get(code)
            if row is None and index < len(ordered):
                row = ordered[index]
                used.add(index)
            if row is None:
                row = {"code": code, "quote_status": "no_tick"}
            row = dict(row)
            row.setdefault("code", code)
            row.setdefault("quote_status", "no_tick")
            result.append(row)

        for index, row in enumerate(ordered):
            if index not in used and str(row.get("code") or "").strip() not in set(codes):
                result.append(row)
        return result

    @classmethod
    def _chain_result(cls, chain: Any, *, key_name: str, requested_keys: list[str], expect_df: bool) -> pd.DataFrame | Any:
        if not expect_df:
            return cls._convert_time_fields(chain)
        rows: list[dict[str, Any]] = []
        if isinstance(chain, dict):
            iterable = chain.items()
        else:
            key = requested_keys[0] if requested_keys else ""
            iterable = [(key, chain)]
        for key, items in iterable:
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                row = dict(item)
                row.setdefault(key_name, key)
                rows.append(row)
        return pd.DataFrame(cls._convert_time_fields(rows))

    @staticmethod
    def _date_result(value: Any) -> pd.Timestamp | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return pd.Timestamp(dt.date.fromisoformat(text[:10]))
        except ValueError as exc:
            raise ValueError(f"invalid date returned by server: {text!r}") from exc

    def _fetch_bar_window_all(
        self,
        code: str,
        *,
        freq: str,
        start: str,
        end: str | None,
        include_live: bool,
    ) -> list[dict[str, Any]]:
        page_limit = 5000
        rows: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            page = self._fetch_bar_window_page(
                code,
                freq=freq,
                limit=page_limit,
                start=start if after is None else None,
                end=end,
                include_live=include_live,
                after=after,
            )
            if not page:
                break
            previous_after = after
            rows.extend(page)
            after = str(page[-1].get("time") or "")
            if not after or after == previous_after or len(page) < page_limit:
                break
        return rows

    def _fetch_bar_window_page(
        self,
        code: str,
        *,
        freq: str,
        limit: int,
        start: str | None,
        end: str | None,
        include_live: bool,
        after: str | None = None,
    ) -> list[dict[str, Any]]:
        params = {
            "code": code,
            "freq": freq,
            "limit": int(limit),
            "include_live": str(bool(include_live)).lower(),
        }
        for key, value in {"start": start, "end": end, "after": after}.items():
            if value:
                params[key] = value
        payload = self._get("/api/v1/bars/window", params)
        return self._convert_time_fields(list(payload.get("bars", [])))

    def get_exchange_info(
        self,
        exchange: list[str] | str = "",
        *,
        fields: list[str] | str | None = None,
        expect_df: bool = True,
    ) -> pd.DataFrame | list[dict[str, Any]]:
        """Query exchange metadata rows."""
        payload = self._get(
            "/api/v1/query/exchange-info",
            self._clean_params(
                {
                    "exchange": self._csv_arg(exchange),
                    "fields": self._csv_arg(fields),
                }
            ),
        )
        return self._rows_result(list(payload.get("rows", [])), expect_df=expect_df)

    def get_instrument_info(
        self,
        instrument: list[str] | str = "",
        *,
        trading_instrument: list[str] | str = "",
        exchange: list[str] | str = "",
        instrument_type: list[str] | str = "",
        night_session_type: list[int] | list[str] | int | str | None = None,
        trading_time: list[str] | str = "",
        has_option: bool | None = None,
        fields: list[str] | str | None = None,
        expect_df: bool = True,
    ) -> pd.DataFrame | list[dict[str, Any]]:
        """Query instrument metadata rows."""
        payload = self._get(
            "/api/v1/query/instrument-info",
            self._clean_params(
                {
                    "instrument": self._csv_arg(instrument),
                    "trading_instrument": self._csv_arg(trading_instrument),
                    "exchange": self._csv_arg(exchange),
                    "instrument_type": self._csv_arg(instrument_type),
                    "night_session_type": self._csv_arg(night_session_type),
                    "trading_time": self._csv_arg(trading_time),
                    "has_option": str(has_option).lower() if has_option is not None else "",
                    "fields": self._csv_arg(fields),
                }
            ),
        )
        return self._rows_result(list(payload.get("rows", [])), expect_df=expect_df)

    def get_future_info(
        self,
        code: list[str] | str = "",
        *,
        instrument: list[str] | str = "",
        trading_instrument: list[str] | str = "",
        exchange: list[str] | str = "",
        active_on: str | dt.date | dt.datetime | pd.Timestamp | None = None,
        listed_date_start: str | dt.date | dt.datetime | pd.Timestamp | None = None,
        listed_date_end: str | dt.date | dt.datetime | pd.Timestamp | None = None,
        expire_date_start: str | dt.date | dt.datetime | pd.Timestamp | None = None,
        expire_date_end: str | dt.date | dt.datetime | pd.Timestamp | None = None,
        is_live: bool | None = None,
        fields: list[str] | str | None = None,
        expect_df: bool = True,
    ) -> pd.DataFrame | list[dict[str, Any]]:
        """Query future contract metadata rows."""
        payload = self._get(
            "/api/v1/query/future-info",
            self._clean_params(
                {
                    "code": self._csv_arg(code),
                    "instrument": self._csv_arg(instrument),
                    "trading_instrument": self._csv_arg(trading_instrument),
                    "exchange": self._csv_arg(exchange),
                    "active_on": self._normalize_datetime_arg(active_on, name="active_on"),
                    "listed_date_start": self._normalize_datetime_arg(listed_date_start, name="listed_date_start"),
                    "listed_date_end": self._normalize_datetime_arg(listed_date_end, name="listed_date_end"),
                    "expire_date_start": self._normalize_datetime_arg(expire_date_start, name="expire_date_start"),
                    "expire_date_end": self._normalize_datetime_arg(expire_date_end, name="expire_date_end"),
                    "is_live": str(is_live).lower() if is_live is not None else "",
                    "fields": self._csv_arg(fields),
                }
            ),
        )
        return self._rows_result(list(payload.get("rows", [])), expect_df=expect_df)

    def get_option_info(
        self,
        code: list[str] | str = "",
        *,
        instrument: list[str] | str = "",
        trading_instrument: list[str] | str = "",
        exchange: list[str] | str = "",
        underlying_code: list[str] | str = "",
        option_type: list[str] | str = "",
        active_on: str | dt.date | dt.datetime | pd.Timestamp | None = None,
        listed_date_start: str | dt.date | dt.datetime | pd.Timestamp | None = None,
        listed_date_end: str | dt.date | dt.datetime | pd.Timestamp | None = None,
        expire_date_start: str | dt.date | dt.datetime | pd.Timestamp | None = None,
        expire_date_end: str | dt.date | dt.datetime | pd.Timestamp | None = None,
        is_live: bool | None = None,
        fields: list[str] | str | None = None,
        expect_df: bool = True,
    ) -> pd.DataFrame | list[dict[str, Any]]:
        """Query option contract metadata rows."""
        payload = self._get(
            "/api/v1/query/option-info",
            self._clean_params(
                {
                    "code": self._csv_arg(code),
                    "instrument": self._csv_arg(instrument),
                    "trading_instrument": self._csv_arg(trading_instrument),
                    "exchange": self._csv_arg(exchange),
                    "underlying_code": self._csv_arg(underlying_code),
                    "option_type": self._csv_arg(option_type),
                    "active_on": self._normalize_datetime_arg(active_on, name="active_on"),
                    "listed_date_start": self._normalize_datetime_arg(listed_date_start, name="listed_date_start"),
                    "listed_date_end": self._normalize_datetime_arg(listed_date_end, name="listed_date_end"),
                    "expire_date_start": self._normalize_datetime_arg(expire_date_start, name="expire_date_start"),
                    "expire_date_end": self._normalize_datetime_arg(expire_date_end, name="expire_date_end"),
                    "is_live": str(is_live).lower() if is_live is not None else "",
                    "fields": self._csv_arg(fields),
                }
            ),
        )
        return self._rows_result(list(payload.get("rows", [])), expect_df=expect_df)

    def get_trading_calendar(
        self,
        start_date: str | dt.date | dt.datetime | pd.Timestamp | None = None,
        end_date: str | dt.date | dt.datetime | pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        """Query trading calendar rows."""
        payload = self._get(
            "/api/v1/query/trading-calendar",
            self._clean_params(
                {
                    "start_date": self._normalize_datetime_arg(start_date, name="start_date"),
                    "end_date": self._normalize_datetime_arg(end_date, name="end_date"),
                }
            ),
        )
        return pd.DataFrame(self._convert_time_fields(list(payload.get("rows", []))))

    def get_previous_trading_date(self, date: str | dt.date | dt.datetime | pd.Timestamp, *, n: int = 1) -> pd.Timestamp | None:
        """Return the nth previous trading date before date."""
        payload = self._get(
            "/api/v1/query/trading-date/previous",
            {
                "date": self._normalize_datetime_arg(date, name="date"),
                "n": int(n),
            },
        )
        return self._date_result(payload.get("date"))

    def get_next_trading_date(self, date: str | dt.date | dt.datetime | pd.Timestamp, *, n: int = 1) -> pd.Timestamp | None:
        """Return the nth next trading date after date."""
        payload = self._get(
            "/api/v1/query/trading-date/next",
            {
                "date": self._normalize_datetime_arg(date, name="date"),
                "n": int(n),
            },
        )
        return self._date_result(payload.get("date"))

    def get_logical_trading_date(self, ts: str | dt.date | dt.datetime | pd.Timestamp | None = None) -> pd.Timestamp | None:
        """Return the server's logical trading date for a timestamp."""
        params = self._clean_params({"ts": self._normalize_datetime_arg(ts, name="ts")})
        payload = self._get("/api/v1/query/trading-date/logical", params)
        return self._date_result(payload.get("date"))

    def get_future_chain(
        self,
        instrument: list[str] | str,
        start_dt: str | dt.date | dt.datetime | pd.Timestamp,
        end_dt: str | dt.date | dt.datetime | pd.Timestamp,
        *,
        expect_df: bool = True,
    ) -> pd.DataFrame | Any:
        """Query active future contracts by instrument and trading date."""
        instruments = self._text_keys(instrument)
        payload = self._get(
            "/api/v1/query/future-chain",
            {
                "instrument": ",".join(instruments),
                "start_dt": self._normalize_datetime_arg(start_dt, name="start_dt"),
                "end_dt": self._normalize_datetime_arg(end_dt, name="end_dt"),
            },
        )
        return self._chain_result(payload.get("chain"), key_name="instrument", requested_keys=instruments, expect_df=expect_df)

    def get_option_chain(
        self,
        future_code: list[str] | str,
        start_dt: str | dt.date | dt.datetime | pd.Timestamp,
        end_dt: str | dt.date | dt.datetime | pd.Timestamp,
        *,
        option_type: str = "",
        expect_df: bool = True,
    ) -> pd.DataFrame | Any:
        """Query active option contracts by underlying future code and trading date."""
        future_codes = self._text_keys(future_code)
        payload = self._get(
            "/api/v1/query/option-chain",
            self._clean_params(
                {
                    "future_code": ",".join(future_codes),
                    "start_dt": self._normalize_datetime_arg(start_dt, name="start_dt"),
                    "end_dt": self._normalize_datetime_arg(end_dt, name="end_dt"),
                    "option_type": option_type,
                }
            ),
        )
        return self._chain_result(payload.get("chain"), key_name="future_code", requested_keys=future_codes, expect_df=expect_df)

    async def subscribe_quotes(
        self,
        codes: list[str] | str,
        *,
        freq: str,
        **legacy: object,
    ) -> AsyncIterator[dict[str, Any]]:
        """Subscribe to tick and bar WebSocket market-data channels."""
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "websockets is required for streaming quotes. Install dependencies with: pip install -r requirements.txt"
            ) from exc

        codes = self._require_codes(codes, legacy)
        channels = self._quote_channels(codes, freq=freq)
        message = {
            "op": "subscribe",
            "channels": channels,
        }
        async with websockets.connect(self._default_ws_url(), additional_headers=self._extra_headers(), ssl=self._ssl_context) as websocket:
            await websocket.send(json.dumps(message))
            async for raw in websocket:
                yield self._convert_time_fields(json.loads(raw))

    def iter_quotes(
        self,
        codes: list[str] | str,
        *,
        freq: str,
        **legacy: object,
    ) -> None:
        """Print WebSocket messages until interrupted."""
        codes = self._require_codes(codes, legacy)

        async def _run() -> None:
            async for item in self.subscribe_quotes(codes, freq=freq):
                timestamped_print(item, flush=True)

        return asyncio.run(_run())

    def listen_quotes(
        self,
        codes: list[str] | str,
        *,
        freq: str,
        on_quote=None,
        on_subscribe=None,
        on_error=None,
        on_message=None,
        duration: float | None = None,
        max_messages: int | None = None,
        stop_event: threading.Event | None = None,
        **legacy: object,
    ) -> None:
        """Use WebSocket quotes with callback-style handlers."""
        codes = self._require_codes(codes, legacy)
        asyncio.run(
            self._listen_quotes_async(
                codes,
                freq=freq,
                on_quote=on_quote,
                on_subscribe=on_subscribe,
                on_error=on_error,
                on_message=on_message,
                duration=duration,
                max_messages=max_messages,
                stop_event=stop_event,
            )
        )

    def start_quote_callbacks(
        self,
        codes: list[str] | str,
        *,
        freq: str,
        on_quote=None,
        on_subscribe=None,
        on_error=None,
        on_message=None,
        daemon: bool = True,
        **legacy: object,
    ) -> RemoteWebSocketSubscription:
        """Start callback-style WebSocket monitoring in a background thread."""
        codes = self._require_codes(codes, legacy)
        stop_event = threading.Event()

        def _target() -> None:
            self.listen_quotes(
                codes,
                freq=freq,
                on_quote=on_quote,
                on_subscribe=on_subscribe,
                on_error=on_error,
                on_message=on_message,
                stop_event=stop_event,
            )

        thread = threading.Thread(target=_target, name="RemoteMarketDataWS", daemon=daemon)
        thread.start()
        return RemoteWebSocketSubscription(thread, stop_event)

    async def _listen_quotes_async(
        self,
        codes: list[str],
        *,
        freq: str,
        on_quote,
        on_subscribe,
        on_error,
        on_message,
        duration: float | None,
        max_messages: int | None,
        stop_event: threading.Event | None,
    ) -> None:
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "websockets is required for streaming quotes. Install dependencies with: pip install -r requirements.txt"
            ) from exc

        channels = self._quote_channels(codes, freq=freq)
        message = {
            "op": "subscribe",
            "channels": channels,
        }
        started = time.time()
        received = 0
        async with websockets.connect(self._default_ws_url(), additional_headers=self._extra_headers(), ssl=self._ssl_context) as websocket:
            await websocket.send(json.dumps(message))
            while True:
                if stop_event is not None and stop_event.is_set():
                    return
                if duration is not None and (time.time() - started) >= float(duration):
                    return
                if max_messages is not None and received >= int(max_messages):
                    return

                try:
                    raw = await asyncio.wait_for(websocket.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue

                item = json.loads(raw)
                received += 1
                self._dispatch_ws_callback_message(
                    item,
                    on_quote=on_quote,
                    on_subscribe=on_subscribe,
                    on_error=on_error,
                    on_message=on_message,
                )

    @staticmethod
    def _quote_channels(codes: list[str], *, freq: str) -> list[str]:
        freq_value = str(freq or "").strip()
        if not freq_value:
            raise ValueError("freq is required")
        if freq_value.lower() == "tick":
            return [f"tick.{code}" for code in codes]
        return [f"bar.{freq_value}.{code}" for code in codes]

    @staticmethod
    def _dispatch_ws_callback_message(
        item: dict[str, Any],
        *,
        on_quote,
        on_subscribe,
        on_error,
        on_message,
    ) -> None:
        converted_full = RemoteMarketDataClient._convert_time_fields(item)

        if on_message is not None:
            on_message(converted_full)

        msg_type = item.get("type")
        op = item.get("op")
        if msg_type in {"tick", "bar"} and on_quote is not None:
            on_quote(converted_full.get("data") or {}, converted_full)
        elif op == "subscribed" and on_subscribe is not None:
            on_subscribe(RemoteMarketDataClient._subscribe_report(item))
        elif msg_type == "error" and on_error is not None:
            on_error(RemoteMarketDataClient._error_report(item))
        elif msg_type == "error" and on_error is not None:
            on_error(RemoteMarketDataClient._error_report(item))

    @staticmethod
    def _subscribe_report(item: dict[str, Any]) -> dict[str, Any]:
        contracts = item.get("contracts")
        if not isinstance(contracts, dict):
            contracts = {}
        return {
            "contracts": RemoteMarketDataClient._convert_time_fields(
                {str(code): dict(info) for code, info in contracts.items() if isinstance(info, dict)}
            ),
            "raw": RemoteMarketDataClient._convert_time_fields(item),
        }

    @staticmethod
    def _error_report(item: dict[str, Any]) -> dict[str, Any]:
        errors = item.get("errors")
        if isinstance(errors, dict):
            grouped = {str(code): dict(info) for code, info in errors.items() if isinstance(info, dict)}
        else:
            channel = str(item.get("channel") or "")
            code = channel.split(".")[-1] if channel else ""
            grouped = {}
            if code:
                grouped[code] = {"code": item.get("code", ""), "message": item.get("message", "")}
        return {
            "errors": RemoteMarketDataClient._convert_time_fields(grouped),
            "raw": RemoteMarketDataClient._convert_time_fields(item),
        }

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = f"?{urlencode(params)}" if params else ""
        request = Request(f"{self.base_url}{path}{query}", headers=self._extra_headers())
        opener = self._opener.open if self._opener is not None else urlopen
        with opener(request, timeout=self.timeout) as response:
            data = response.read().decode("utf-8")
        return json.loads(data)

    def _default_ws_url(self) -> str:
        if self.base_url.startswith("https://"):
            base = "wss://" + self.base_url[len("https://") :] + "/ws/v1/market-data"
        elif self.base_url.startswith("http://"):
            base = "ws://" + self.base_url[len("http://") :] + "/ws/v1/market-data"
        else:
            base = self.base_url.rstrip("/") + "/ws/v1/market-data"
        params = {}
        if self.api_user:
            params["user"] = self.api_user
        if self.api_token:
            params["token"] = self.api_token
        query = urlencode(params)
        return f"{base}?{query}" if query else base
