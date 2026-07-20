from datetime import datetime, timezone


def utc_now() -> datetime:
    """
    返回带时区的UTC时间。

    数据库存储统一使用UTC时间，
    前端展示时再转换为本地时间。
    """

    return datetime.now(timezone.utc)