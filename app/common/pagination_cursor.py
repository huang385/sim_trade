import base64
import binascii
import json
import time
from dataclasses import dataclass
from typing import Mapping

from app.common.exceptions import BusinessValidationError


CURSOR_VERSION = 1
DEFAULT_CURSOR_MAX_AGE_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class DecodedCursor:
    """校验完成后的不透明游标内容。"""

    before_id: int
    issued_at: int


def encode_cursor(
    *,
    kind: str,
    before_id: int,
    filters: Mapping[str, str],
    now: int | None = None,
) -> str:
    """把内部主键和查询范围编码为URL安全的不透明字符串。"""

    payload = {
        "v": CURSOR_VERSION,
        "kind": kind,
        "before_id": before_id,
        "filters": dict(sorted(filters.items())),
        "issued_at": int(time.time()) if now is None else now,
    }
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(
    cursor: str,
    *,
    expected_kind: str,
    expected_filters: Mapping[str, str],
    max_age_seconds: int = DEFAULT_CURSOR_MAX_AGE_SECONDS,
    now: int | None = None,
) -> DecodedCursor:
    """
    解码并校验游标版本、查询范围和有效期。

    游标只能用于创建它的接口和过滤条件，防止把其他账户或成交查询的游标
    混用。格式错误与过期分别返回明确业务错误。
    """

    try:
        normalized = cursor.strip()
        if not normalized:
            raise ValueError("empty cursor")
        padding = "=" * (-len(normalized) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode(
                normalized + padding
            ).decode("utf-8")
        )
    except (
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
        binascii.Error,
    ) as exc:
        raise BusinessValidationError(
            "分页游标格式错误",
            error_code="INVALID_CURSOR",
        ) from exc

    expected_keys = {
        "v",
        "kind",
        "before_id",
        "filters",
        "issued_at",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != expected_keys
        or payload.get("v") != CURSOR_VERSION
        or payload.get("kind") != expected_kind
        or type(payload.get("before_id")) is not int
        or payload["before_id"] <= 0
        or type(payload.get("issued_at")) is not int
        or payload.get("filters")
        != dict(sorted(expected_filters.items()))
    ):
        raise BusinessValidationError(
            "分页游标与当前查询不匹配",
            error_code="INVALID_CURSOR",
        )

    current = int(time.time()) if now is None else now
    issued_at = payload["issued_at"]
    if issued_at > current + 60:
        raise BusinessValidationError(
            "分页游标时间无效",
            error_code="INVALID_CURSOR",
        )
    if current - issued_at > max_age_seconds:
        raise BusinessValidationError(
            "分页游标已过期",
            error_code="CURSOR_EXPIRED",
        )
    return DecodedCursor(
        before_id=payload["before_id"],
        issued_at=issued_at,
    )
