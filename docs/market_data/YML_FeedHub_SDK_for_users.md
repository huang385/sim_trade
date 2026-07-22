# YML FeedHub SDK 使用说明

## 简介

面向下游策略、研究和监控程序的 Python 客户端封装。SDK 通过 REST 获取一次性查询结果，通过 WebSocket 订阅实时行情推送。

当前 SDK 主要覆盖五类能力：

- 行情查询：最新 tick、K 线窗口、批量 K 线窗口。
- 实时订阅：按 `tick` 或指定 bar 频率订阅 WebSocket 行情。
- 合约校验：检查合约是否存在于本地元信息快照，以及是否 `is_live`。
- 元信息查询：交易所、品种、期货合约、期权合约信息。
- 交易日与合约链：交易日历、前后交易日、逻辑交易日、期货链和期权链。

默认返回类型以 `pandas.DataFrame` 为主，便于下游直接进入研究、风控或策略计算流程；需要原始结构时可使用各方法的 `expect_df=False`。

## 文档信息

| 项目 | 内容 |
|---|---|
| Document | `YML_FeedHub_SDK_for_users.md` |
| SDK class | `RemoteMarketDataClient` |
| Version | `1.0.0` |
| Updated at | `2026/06/10` |

## 改动日志

| 日期 | 版本 | 主要改动 |
|---|---:|---|
| 2026/06/10 | `1.0.0` | 初版 SDK 文档，覆盖连接、行情查询、元信息查询、交易日工具、合约链和 WebSocket 行情监听 |

## 当前公开方法速查

| 类别 | 方法 | 主要用途 | 默认返回 |
|---|---|---|---|
| Client | `RemoteMarketDataClient(...)` | 创建 SDK 客户端，配置 `base_url`、`timeout`、`api_user`、`api_token`、`verify_ssl` | client object |
| 健康检查 | `health(expect_df=True)` | 查询 HTTP 服务是否存活 | `pd.DataFrame` |
| 运行状态 | `get_status(expect_df=True)` | 查询服务、ingestion、writer、WS、request manager 等运行状态 | `pd.DataFrame` |
| 运行状态 | `get_runtime(expect_df=True)` | `get_status()` 兼容别名 | `pd.DataFrame` |
| 最新 tick | `get_latest_tick(code, expect_df=True)` | 查询单合约最新 tick；不存在合约返回占位/`None` | `pd.DataFrame` |
| 批量最新 tick | `get_latest_ticks(codes, expect_df=True)` | 批量查询最新 tick；每个请求 code 保留一行 | `pd.DataFrame` |
| K 线 | `get_bar_window(code, freq="1m", limit=200, start=None, end=None, include_live=True, expect_df=True)` | 查询单合约 K 线窗口；时间字段为 right-label `time` | `pd.DataFrame` |
| 批量K 线 | `get_bar_windows(codes, freq="1m", limit=200, start=None, end=None, include_live=True, expect_df=True)` | 批量查询 K 线窗口；不存在合约返回空表 | `dict[str, pd.DataFrame]` |
| 合约当前状态校验 | `validate_contracts(codes, expect_df=True)` | 检查合约是否存在于 `future_info/option_info`（是否合法），以及是否 `is_live`（是否到期） | `pd.DataFrame` |
| 元信息 | `get_exchange_info(exchange="", fields=None, expect_df=True)` | 查询交易所元信息 | `pd.DataFrame` |
| 元信息 | `get_instrument_info(...)` | 查询品种元信息，支持交易时间、夜盘类型等筛选 | `pd.DataFrame` |
| 元信息 | `get_future_info(...)` | 查询期货合约元信息，支持 `is_live`、上市/到期日期等筛选 | `pd.DataFrame` |
| 元信息 | `get_option_info(...)` | 查询期权合约元信息，支持 `is_live`、标的、期权类型等筛选 | `pd.DataFrame` |
| 交易日 | `get_trading_calendar(start_date=None, end_date=None, ...)` | 查询交易日历 | `pd.DataFrame` |
| 交易日 | `get_previous_trading_date(date, n=1)` | 查询严格早于指定日期的第 `n` 个交易日 | `pd.Timestamp \| None` |
| 交易日 | `get_next_trading_date(date, n=1)` | 查询严格晚于指定日期的第 `n` 个交易日 | `pd.Timestamp \| None` |
| 交易日 | `get_logical_trading_date(ts=None)` | 按服务端逻辑计算交易日 | `pd.Timestamp \| None` |
| 合约链 | `get_future_chain(instrument, start_dt, end_dt, expect_df=True)` | 查询期货链 | `pd.DataFrame` |
| 合约链 | `get_option_chain(underlying_code, start_dt, end_dt, expect_df=True)` | 查询期权链 | `pd.DataFrame` |
| WebSocket | `start_quote_callbacks(codes, freq, on_quote=None, on_subscribe=None, on_message=None, on_error=None, ...)` | 后台线程方式订阅 tick 或 bar 行情 | `RemoteWebSocketSubscription` |

## 1. 连接配置

先定义三个全局变量：`BASE_URL`、`API_USER` 和 `API_TOKEN`。本机 loopback 访问可以不填用户和 token；局域网/公网访问必须填写 `config/api_users.json` 中的 username + token。

```python
# 按实际访问方式三选一：
BASE_URL = "http://127.0.0.1:54111"        # 本机访问，可免鉴权
# BASE_URL = "http://192.168.11.153:54111" # 局域网访问
# BASE_URL = "https://frp-ski.com:28360"   # 公网访问

API_USER = ""                            # 填用户名"
API_TOKEN = ""                           # 填用户 token
```

注意：

- 公司内部除特殊机器外，一律使用局域网访问方式
- `BASE_URL` 是行情服务 HTTP API 的根地址，不包含 `/api/v1/...` 路径。
- 远程 REST 鉴权使用 `X-Api-User` + `X-Api-Token`，需要管理员进行分发。
- 远程 WebSocket 鉴权使用 `?user=...&token=...`。
- 非交易时段仍可查询历史 bars、元信息、交易日和 chain；latest tick 可能返回无 tick，WS 会返回 idle/status 消息而不是持续推送 live 行情。

## 2. SDK 方式

SDK 类在：

```python
from remote_sdk_client import RemoteMarketDataClient
```

## 2.1 创建 Client

签名：

```python
RemoteMarketDataClient(
    base_url: str = "http://127.0.0.1:54111",
    *,
    timeout: float = 3.0,
    api_user: str = "",
    api_token: str = "",
    verify_ssl: bool = True,
)
```

参数：

| 参数         | 类型   | 含义                                        |
| ------------ | ------ | ------------------------------------------- |
| `base_url`   | `str`  | API 服务地址，传入前文定义的 `BASE_URL`     |
| `timeout`    | `float` | REST 请求超时秒数，超时会抛异常             |
| `api_user`   | `str`  | 远程访问用户名              |
| `api_token`  | `str`  | 远程访问用户 token          |
| `verify_ssl` | `bool` | HTTPS 证书校验；**外网使用需要设定`False`** |

创建示例：

```python
# 局域网/公网访问：使用 config/api_users.json 中的用户。
client = RemoteMarketDataClient(
    BASE_URL,
    api_user=API_USER,
    api_token=API_TOKEN,
    verify_ssl=False,
)
```

## 2.2 查询

### 2.2.1 `health()`

签名：

```python
health(*, expect_df: bool = True) -> pd.DataFrame | dict
```

说明：请求 `GET /health`，用于判断 HTTP 服务是否存活。本机访问不需要 token；远程访问仍需要用户 token。

返回字段常见结构：

| 字段                | 类型    | 含义                                           |
| ------------------- | ------- | ---------------------------------------------- |
| `ok`                | `bool`  | HTTP 服务是否正常响应                          |
| `ready`             | `bool`  | HTTP API 是否可服务；全天候模式下通常为 `True` |
| `connected`         | `bool`  | XT SDK ingestion 是否连接                      |
| `ingestion_running` | `bool`  | 当前是否处于行情采集运行状态                   |
| `started_at`        | `pd.Timestamp` | API server 启动时间                            |
| `uptime_seconds`    | `float`        | 运行秒数                                       |
| `server_time`       | `pd.Timestamp` | 服务端当前时间                                 |

示例：

```python
health = client.health()
print(health.iloc[0]["ok"], health.iloc[0].get("ready"))

raw = client.health(expect_df=False)
print(raw["ok"])
```

### 2.2.2 `get_status()` / `get_runtime()`

签名：

```python
get_status(*, expect_df: bool = True) -> pd.DataFrame | dict
get_runtime(*, expect_df: bool = True) -> pd.DataFrame | dict
```

说明：请求 `GET /api/v1/status`。`get_runtime()` 是兼容别名。用于判断服务各种类的状态。

返回字段：

| 字段               | 类型   | 含义                       |
| ------------------ | ------ | -------------------------- |
| `ok`               | `bool` | status 请求是否成功        |
| `connected`        | `bool` | XT SDK 是否连接            |
| `logged_in`        | `bool` | 是否登录成功               |
| `last_error`       | `str`  | 最近错误                   |
| `latest_tick_time` | `pd.Timestamp` | 最近一次全局 tick 时间     |
| `latest_tick_code` | `str`  | 最近一次全局 tick 合约     |
| `allcode`          | `dict` | allCode 订阅摘要           |
| `subscriptions`    | `dict` | SubscribeManager 状态      |
| `consumer`         | `dict` | TickConsumer 状态          |
| `tick_writer`      | `dict` | tick 写库状态              |
| `bar_engine`       | `dict` | bar engine 状态            |
| `bar_writer`       | `dict` | bar 写库状态               |
| `data_service`     | `dict` | bar 查询服务状态           |
| `clickhouse`       | `dict` | ClickHouse 状态和最近错误  |
| `request_manager`  | `dict` | REST/WS 请求统计           |
| `ws`               | `dict` | WS 连接、channel、队列统计 |
| `server`           | `dict` | API server 进程状态        |

示例：

```python
status = client.get_status()
row = status.iloc[0]
print(row["connected"], row["logged_in"])
print(row["latest_tick_code"], row["latest_tick_time"])
print(row["tick_writer"].get("insert_failed_total"))

raw = client.get_status(expect_df=False)
print(raw["connected"])
```

### 2.2.3 `get_latest_tick(code)`

签名：

```python
get_latest_tick(
    code: str = "",
    *,
    expect_df: bool = True,
) -> pd.DataFrame | dict | None
```

参数：

| 参数        | 类型   | 含义                                                         |
| ----------- | ------ | ------------------------------------------------------------ |
| `code`      | `str`  | 单个合约代码，例如 `"AG2606"`                                |
| `expect_df` | `bool` | 默认 `True` 返回一行 DataFrame；`False` 返回结构 `dict | None` |

默认返回类型：`pd.DataFrame`。有 tick 时返回一行 tick 字段；没有 tick 时返回一行占位记录，至少包含 `code` 和 `quote_status="no_tick"`。

`expect_df=False` 时，SDK 会从 REST payload 的 `ticks[0]["tick"]` 取出内部 tick 返回；有最新 tick 时返回 `dict`，没有最新 tick 时返回 `None`。

返回结构（所有时间字段由 SDK 统一转换为 `pd.Timestamp`）：

```python
{
    "code": "AG2606",
    "exchange": "SHFE",
    "trading_day": Timestamp("2026-06-01 00:00:00"),
    "event_time": Timestamp("2026-06-01 09:30:01.500"),
    "last_price": 8000.0,
    "bid_price_1": 7999.0,
    "ask_price_1": 8001.0,
    "cum_volume": 12345,
    "cum_turnover": 123456789.0,
    "open_interest": 100000.0,
    "sequence_id": 1001
}
```

示例：

```python
tick = client.get_latest_tick("AG2606")
if not tick.empty and tick.iloc[0]["quote_status"] != "no_tick":
    print(tick.iloc[0]["code"], tick.iloc[0]["last_price"], tick.iloc[0]["event_time"])

raw = client.get_latest_tick("AG2606", expect_df=False)
if raw is not None:
    print(raw["last_price"])
```

### 2.2.4 `get_latest_ticks(codes)`

签名：

```python
get_latest_ticks(
    codes: list[str] | str,
    *,
    expect_df: bool = True,
) -> pd.DataFrame | dict[str, dict | None]
```

参数：

| 参数        | 类型                         | 含义                                                         |
| ----------- | ---------------------------- | ------------------------------------------------------------ |
| `codes`     | `list[str]` 或逗号分隔 `str` | 多个合约代码，例如 `["AG2606", "AU2606"]` 或 `"AG2606,AU2606"` |
| `expect_df` | `bool`                       | 默认 `True` 返回 DataFrame；`False` 返回结构 `dict[str, dict | None]` |

默认返回类型：`pd.DataFrame`，每个请求 code 保留一行；无 tick 的合约也保留一行，并带 `quote_status="no_tick"`。

`expect_df=False` 时返回 `dict[str, dict | None]`。key 是合约代码，value 是该合约最新 tick dict；如果该合约当前没有 tick，则 value 为 `None`。

返回示例：

```python
df = client.get_latest_ticks(["AG2606", "AU2606"])
# columns: code, exchange, quote_status, trading_day, event_time, last_price, ...

raw = client.get_latest_ticks(["AG2606", "AU2606"], expect_df=False)
# {"AG2606": {...}, "AU2606": None}
```

示例：

```python
rows = client.get_latest_ticks(["AG2606", "AU2606"])
for _, row in rows.iterrows():
    print(row["code"], row.get("quote_status"), row.get("last_price"))
```

### 2.2.5 `get_bar_window(code, ...)`

签名：

```python
get_bar_window(
    code: str = "",
    *,
    freq: str = "1m",
    limit: int | None = 200,
    start: str | datetime.date | datetime.datetime | pd.Timestamp | None = None,
    end: str | datetime.date | datetime.datetime | pd.Timestamp | None = None,
    include_live: bool = True,
    expect_df: bool = True,
) -> pd.DataFrame | list[dict]
```

参数：

| 参数           | 类型                                                         | 含义                                                         |
| -------------- | ------------------------------------------------------------ | ------------------------------------------------------------ |
| `code`         | `str`                                                        | 合约代码                                                     |
| `freq`         | `str`                                                        | bar 周期，支持 `1m`、`5m`、`15m`、`30m`、`1h`                |
| `limit`        | `int | None`                                                 | 不为 `None` 时，从 `end` 向前数，最多返回 `limit` 根；为 `None` 时返回完整 `start/end` 窗口 |
| `start`        | `str | datetime.date | datetime.datetime | pd.Timestamp | None` | 起始时间；为 `None` 表示不设置起始边界。**不要让`start`和`limit`同时为`None`** |
| `end`          | `str | datetime.date | datetime.datetime | pd.Timestamp | None` | 结束时间；为 `None` 表示当前最新时间                         |
| `include_live` | `bool`                                                       | 是否合并内存中的 forming/correctable/frozen bar。一般选择    |
| `expect_df`    | `bool`                                                       | 默认 `True` 返回 DataFrame；`False` 返回旧结构 `list[dict]`  |

默认返回类型：`pd.DataFrame`，每行是一根 bar。`expect_df=False` 时返回 `list[dict]`。

说明：

- K 线使用 right-label。查询窗口里 `start` 是排除边界，返回 `time > start`；`end` 是包含边界，返回 `time <= end`。例如 `start="2026-06-01 11:00:00"` 不会包含 label 为 `11:00` 的 1m bar。
- `limit` 不为 `None` 时，SDK 返回 `start/end` 范围内最多 `limit` 根 bar，并从 `end` 向前数；如果 `start` 不为 `None`，则结果会被 `start` 截断。
- `limit=None` 时必须传 `start`，SDK 会自动分页取完整 `start/end` 窗口。
- 1m bar 默认有 30 秒 correction window。`include_live=True` 时，返回结果会包含内存中的 forming/correctable bar；这些 bar 是当前最正确版本，但在 correction window 内仍可能被 late tick 修正。`include_live=False` 或纯 ClickHouse 查询只能看到已经 frozen/written 的 bar。**如果你并不理解实际机理，请使用默认值。**

`start/end` 的时间入参支持：

| 输入类型                              | 示例                                         |
| ------------------------------------- | -------------------------------------------- |
| `YYYYMMDDHHmmss` 字符串               | `"20260601090000"`                           |
| `YYYY-MM-DD HH:mm:ss` 字符串          | `"2026-06-01 09:00:00"`                      |
| ISO 字符串                            | `"2026-06-01T09:00:00"`                      |
| 其他常见日期字符串                    | `"2026/06/01 09:00:00"`、`"20260601 090000"` |
| `datetime.datetime` / `datetime.date` | `datetime.datetime(2026, 6, 1, 9, 0, 0)`     |
| `pd.Timestamp`                        | `pd.Timestamp("2026-06-01 09:00:00")`        |

SDK 会把这些时间统一转换为 ISO 字符串再发给 REST；解析不了的字符串会抛 `ValueError`。

bar 常见字段（所有时间字段由 SDK 统一转换为 `pd.Timestamp`）：

| 字段                  | 类型    | 含义                                               |
| --------------------- | ------- | -------------------------------------------------- |
| `code`                | `str`   | 合约代码                                           |
| `exchange`            | `str`   | 交易所                                             |
| `freq`                | `str`   | bar 周期                                           |
| `trading_day`         | `pd.Timestamp` | 交易日                                     |
| `time`                | `pd.Timestamp` | bar right-label 时间                         |
| `open/high/low/close` | `float` | OHLC                                               |
| `cum_volume`          | `int`   | 交易所回传累计成交量                               |
| `cum_turnover`        | `float` | 交易所回传累计成交额                               |
| `open_interest`       | `float` | 持仓量                                             |
| `bar_status`          | `str`   | `forming` 或 `closed`                              |
| `source`              | `str`   | `clickhouse`、`memory_current`、`memory_frozen` 等 |
| `sequence_id`         | `int`   | API 序列号                                         |
| `version`             | `int`   | 前端增量更新用版本号                               |

示例：

```python
bars = client.get_bar_window("AG2606", freq="1m", limit=200, include_live=True)
for _, bar in bars.tail(3).iterrows():
    print(bar["time"], bar["close"], bar["bar_status"], bar.get("source_mode"))
```

时间窗口示例，最多取 200 根：

```python
bars = client.get_bar_window(
    "AG2606",
    freq="1m",
    limit=200,
    start="2026-06-01T09:00:00",
    end="2026-06-01T10:00:00",
)
```

完整时间窗口示例：

```python
bars = client.get_bar_window(
    "AG2606",
    freq="1m",
    limit=None,
    start="2026-06-01T09:00:00",
    end="2026-06-01T10:00:00",
)
```

### 2.2.6 `get_bar_windows(codes, ...)`

签名：

```python
get_bar_windows(
    codes: list[str] | str,
    *,
    freq: str = "1m",
    limit: int | None = 200,
    start: str | datetime.date | datetime.datetime | pd.Timestamp | None = None,
    end: str | datetime.date | datetime.datetime | pd.Timestamp | None = None,
    include_live: bool = True,
    expect_df: bool = True,
) -> dict[str, pd.DataFrame] | dict[str, list[dict]]
```

参数与 `get_bar_window()` 相同，但 `codes` 接受 `list[str]` 或逗号分隔字符串。

默认返回类型：`dict[str, pd.DataFrame]`。key 是合约代码，value 是该合约自己的 bar DataFrame。

`expect_df=False` 时返回 `dict[str, list[dict]]`。key 是合约代码，value 是该合约的 bar window。

示例：

```python
windows = client.get_bar_windows(["AG2606", "AU2606"], freq="1m", limit=200)
print(windows["AG2606"].tail())

raw = client.get_bar_windows(["AG2606", "AU2606"], freq="1m", limit=200, expect_df=False)
for code, bars in raw.items():
    print(code, len(bars))
```

### 2.2.7 合约有效性检查

行情查询和订阅接口会容忍缺失合约：

- `get_latest_tick()` / `get_latest_ticks()`：缺失合约返回 `quote_status="contract_not_found"`，`tick=None`；SDK `expect_df=False` 时该 code 对应 `None`。
- `get_bar_window()` / `get_bar_windows()`：缺失合约返回空 DataFrame / 空 list。
- `start_quote_callbacks()`：缺失合约会在 `on_subscribe(report)` 的 `contracts[code]` 中标记 `exists=False/subscribed=False`，不会触发 `on_error`，也不会有该合约的行情回调。

如果策略需要在启动前主动校验合约是否存在、是否 `is_live`，使用下面的方法。

#### `validate_contracts(codes)`

签名：

```python
validate_contracts(
    codes: list[str] | str,
    *,
    expect_df: bool = True,
) -> pd.DataFrame | dict[str, dict]
```

返回字段：

| 字段 | 类型 | 含义 |
| ---- | ---- | ---- |
| `input_code` | `str` | 用户传入的原始 code |
| `standard_code` | `str` | 标准化后的 code；不存在时通常等于输入 code 的规范大写形式 |
| `trading_code` | `str` | 行情/交易侧使用的合约代码；不存在时为空字符串 |
| `exists` | `bool` | 是否存在于本地 `future_info.json` / `option_info.json` |
| `is_live` | `bool` | 是否为当前 live 合约；不存在合约固定为 `False` |

示例：

```python
checks = client.validate_contracts(["TL2608", "AU2606"], expect_df=False)
if not checks["TL2608"]["exists"]:
    print("TL2608 not found")
if checks["AU2606"]["is_live"]:
    print("AU2606 is live")
```

### 2.2.8 元信息、交易日和链查询

参数规则：

- 主查询键可以按位置传入，例如 `client.get_exchange_info("DCE")`、`client.get_future_info("AG2606")`、`client.get_option_info("AG2606C8000")`。
- 多筛选条件建议使用参数名，例如 `client.get_future_info(instrument="AG", exchange="SHFE")`。这样比按顺序塞一长串参数更不容易传错。
- 文本过滤参数支持单个字符串、字符串 list，REST 也支持逗号分隔字符串；list/逗号分隔表示 OR 语义，例如 `instrument=["AG", "AU"]` 表示 AG 或 AU。
- `get_exchange_info()`、`get_instrument_info()`、`get_future_info()`、`get_option_info()` 默认返回 `pd.DataFrame`；传 `expect_df=False` 时返回原始 `list[dict]`。
- `get_trading_calendar()` 固定返回 `pd.DataFrame`。
- `get_previous_trading_date()`、`get_next_trading_date()`、`get_logical_trading_date()` 返回 `pd.Timestamp | None`。
- 日期参数支持 SDK 时间入参格式：`YYYYMMDDHHmmss`、`YYYYMMDD`、`YYYY-MM-DD HH:mm:ss`、ISO 字符串、`datetime`、`pd.Timestamp` 等。
- 字段具体含义详见数据库文档

#### `get_exchange_info(...)`

签名：

```python
get_exchange_info(
    exchange: list[str] | str = "",
    *,
    fields: list[str] | str | None = None,
    expect_df: bool = True,
) -> pd.DataFrame | list[dict]
```

参数：

| 参数        | 类型                     | 含义                                                         |
| ----------- | ------------------------ | ------------------------------------------------------------ |
| `exchange`  | `str | list[str]`        | 交易所代码，例如 `"DCE"` 或 `["DCE", "SHFE"]`；空字符串表示不过滤 |
| `fields`    | `list[str] | str | None` | 返回字段；`None` 返回默认字段，也可传 `"exchange,name"`      |
| `expect_df` | `bool`                   | 默认 `True` 返回 `pd.DataFrame`；`False` 返回 `list[dict]`   |

可选字段：

```python
["exchange", "name", "xtdata_exchange", "max_trading_time"]
```

返回示例，默认是 DataFrame：

```python
df = client.get_exchange_info(["DCE", "SHFE"], fields=["exchange", "name"])
```

如需原始 list：

```python
[
    {
        "exchange": "DCE",
        "name": "大连商品交易所",
        "xtdata_exchange": "DF",
        "max_trading_time": "23:00:00",
    }
]
```

#### `get_instrument_info(...)`

签名：

```python
get_instrument_info(
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
) -> pd.DataFrame | list[dict]
```

参数：

| 参数                 | 类型                     | 含义                                                       |
| -------------------- | ------------------------ | ---------------------------------------------------------- |
| `instrument`         | `str | list[str]`        | 内部统一品种代码，例如 `"AG"` 或 `["AG", "AU"]`            |
| `trading_instrument` | `str | list[str]`        | 交易所/行情源品种代码                                      |
| `exchange`           | `str | list[str]`        | 交易所代码                                                 |
| `instrument_type`    | `str | list[str]`        | 品种类型，例如 `commodity`、`index`、`government`          |
| `night_session_type` | `int | str | list`       | 夜盘类型，支持单值或 list                                  |
| `trading_time`       | `str | list[str]`        | 对 `trading_time` 序列化文本做包含匹配                     |
| `has_option`         | `bool | None`            | 是否有期权；`None` 表示不过滤                              |
| `fields`             | `list[str] | str | None` | 返回字段                                                   |
| `expect_df`          | `bool`                   | 默认 `True` 返回 `pd.DataFrame`；`False` 返回 `list[dict]` |

可选字段：

```python
[
    "instrument", "trading_instrument", "exchange", "instrument_type",
    "name", "has_option", "night_session_type",
    "future_contract_multiplier", "option_contract_multiplier",
    "future_tick_size", "option_tick_size", "trading_time",
]
```

返回示例：

```python
rows = client.get_instrument_info(
    instrument=["AG", "AU"],
    fields=["instrument", "exchange", "night_session_type", "has_option"],
)
```

#### `get_future_info(...)`

签名：

```python
get_future_info(
    code: list[str] | str = "",
    *,
    instrument: list[str] | str = "",
    trading_instrument: list[str] | str = "",
    exchange: list[str] | str = "",
    active_on: str | datetime.date | datetime.datetime | pd.Timestamp | None = None,
    listed_date_start: str | datetime.date | datetime.datetime | pd.Timestamp | None = None,
    listed_date_end: str | datetime.date | datetime.datetime | pd.Timestamp | None = None,
    expire_date_start: str | datetime.date | datetime.datetime | pd.Timestamp | None = None,
    expire_date_end: str | datetime.date | datetime.datetime | pd.Timestamp | None = None,
    is_live: bool | None = None,
    fields: list[str] | str | None = None,
    expect_df: bool = True,
) -> pd.DataFrame | list[dict]
```

参数：

| 参数                    | 类型                                                         | 含义                                                         |
| ----------------------- | ------------------------------------------------------------ | ------------------------------------------------------------ |
| `code`                  | `str | list[str]`                                            | 期货合约代码，例如 `"AG2606"` 或 `["AG2606", "AU2606"]`；可作为第一个位置参数 |
| `instrument`            | `str | list[str]`                                            | 品种代码，例如 `"AG"` 或 `["AG", "AU"]`                      |
| `trading_instrument`    | `str | list[str]`                                            | 交易品种代码                                                 |
| `exchange`              | `str | list[str]`                                            | 交易所代码                                                   |
| `active_on`             | `str | datetime.date | datetime.datetime | pd.Timestamp | None` | 只返回在该日期仍存续的合约                                   |
| `listed_date_start/end` | `str | datetime.date | datetime.datetime | pd.Timestamp | None` | 上市日期范围                                                 |
| `expire_date_start/end` | `str | datetime.date | datetime.datetime | pd.Timestamp | None` | 到期日期范围                                                 |
| `is_live`               | `bool | None`                                                | 是否只返回当前 live 合约；`None` 表示不过滤                  |
| `fields`                | `list[str] | str | None`                                     | 返回字段                                                     |
| `expect_df`             | `bool`                                                       | 默认 `True` 返回 `pd.DataFrame`；`False` 返回 `list[dict]`   |

可选字段：

```python
[
    "code", "instrument_type", "trading_code", "instrument",
    "trading_instrument", "exchange", "delivery_month", "name",
    "listed_date", "expire_date", "start_delivery_date", "end_delivery_date",
    "contract_multiplier", "tick_size", "long_margin_ratio",
    "short_margin_ratio", "market_tplus", "trading_time", "is_live",
]
```

示例：

```python
rows = client.get_future_info(
    instrument="AG",
    is_live=True,
    fields=["code", "instrument", "exchange", "expire_date", "is_live"],
)
```

`rows` 默认是 DataFrame。如需 list：

```python
[
    {
        "code": "AG2606",
        "instrument": "AG",
        "exchange": "SHFE",
        "listed_date": Timestamp("2025-06-17 00:00:00"),
        "expire_date": Timestamp("2026-06-15 00:00:00"),
        "contract_multiplier": 15.0,
        "tick_size": 1.0,
        "is_live": True,
    }
]
```

#### `get_option_info(...)`

签名：

```python
get_option_info(
    code: list[str] | str = "",
    *,
    instrument: list[str] | str = "",
    trading_instrument: list[str] | str = "",
    exchange: list[str] | str = "",
    underlying_code: list[str] | str = "",
    option_type: list[str] | str = "",
    active_on: str | datetime.date | datetime.datetime | pd.Timestamp | None = None,
    listed_date_start: str | datetime.date | datetime.datetime | pd.Timestamp | None = None,
    listed_date_end: str | datetime.date | datetime.datetime | pd.Timestamp | None = None,
    expire_date_start: str | datetime.date | datetime.datetime | pd.Timestamp | None = None,
    expire_date_end: str | datetime.date | datetime.datetime | pd.Timestamp | None = None,
    is_live: bool | None = None,
    fields: list[str] | str | None = None,
    expect_df: bool = True,
) -> pd.DataFrame | list[dict]
```

参数：

| 参数                    | 类型                                                         | 含义                                                         |
| ----------------------- | ------------------------------------------------------------ | ------------------------------------------------------------ |
| `code`                  | `str | list[str]`                                            | 期权合约代码；可作为第一个位置参数                           |
| `instrument`            | `str | list[str]`                                            | 品种代码                                                     |
| `trading_instrument`    | `str | list[str]`                                            | 交易品种代码                                                 |
| `exchange`              | `str | list[str]`                                            | 交易所代码                                                   |
| `underlying_code`       | `str | list[str]`                                            | 标的期货合约代码，例如 `"AG2606"`                            |
| `option_type`           | `str | list[str]`                                            | `"C"` 看涨，`"P"` 看跌，也可传 `["C", "P"]`；空字符串表示不过滤 |
| `active_on`             | `str | datetime.date | datetime.datetime | pd.Timestamp | None` | 只返回在该日期仍存续的合约                                   |
| `listed_date_start/end` | `str | datetime.date | datetime.datetime | pd.Timestamp | None` | 上市日期范围                                                 |
| `expire_date_start/end` | `str | datetime.date | datetime.datetime | pd.Timestamp | None` | 到期日期范围                                                 |
| `is_live`               | `bool | None`                                                | 是否只返回当前 live 合约；`None` 表示不过滤                  |
| `fields`                | `list[str] | str | None`                                     | 返回字段                                                     |
| `expect_df`             | `bool`                                                       | 默认 `True` 返回 `pd.DataFrame`；`False` 返回 `list[dict]`   |

可选字段：

```python
[
    "code", "instrument_type", "trading_code", "instrument",
    "trading_instrument", "exchange", "underlying_code",
    "underlying_trading_code", "delivery_month", "name",
    "listed_date", "expire_date", "contract_multiplier", "tick_size",
    "strike_price", "option_type", "exercise_type", "market_tplus",
    "trading_time", "is_live",
]
```

返回示例：

```python
[
    {
        "code": "AG2606C8000",
        "instrument": "AG",
        "exchange": "SHFE",
        "underlying_code": "AG2606",
        "strike_price": 8000.0,
        "option_type": "C",
        "expire_date": Timestamp("2026-06-15 00:00:00"),
    }
]
```

#### `get_trading_calendar(...)`

签名：

```python
get_trading_calendar(
    start_date: str | datetime.date | datetime.datetime | pd.Timestamp | None = None,
    end_date: str | datetime.date | datetime.datetime | pd.Timestamp | None = None,
) -> pd.DataFrame
```

参数：

| 参数         | 类型                                                         | 含义                           |
| ------------ | ------------------------------------------------------------ | ------------------------------ |
| `start_date` | `str | datetime.date | datetime.datetime | pd.Timestamp | None` | 起始日期；可作为第一个位置参数 |
| `end_date`   | `str | datetime.date | datetime.datetime | pd.Timestamp | None` | 结束日期；可作为第二个位置参数 |

可选字段固定为：

```python
["date", "is_trading", "has_night_session"]
```

返回示例，固定为 DataFrame：

```python
calendar = client.get_trading_calendar("20260603", "20260604")
```

等价记录示例：

```python
[
    {"date": "2026-06-03", "is_trading": True, "has_night_session": True},
    {"date": "2026-06-04", "is_trading": True, "has_night_session": True},
]
```

#### 交易日方法

签名：

```python
get_previous_trading_date(date: str | datetime.date | datetime.datetime | pd.Timestamp, *, n: int = 1) -> pd.Timestamp | None
get_next_trading_date(date: str | datetime.date | datetime.datetime | pd.Timestamp, *, n: int = 1) -> pd.Timestamp | None
get_logical_trading_date(ts: str | datetime.date | datetime.datetime | pd.Timestamp | None = None) -> pd.Timestamp | None
```

说明：

- `get_previous_trading_date()` 返回严格早于 `date` 的第 `n` 个交易日。
- `get_next_trading_date()` 返回严格晚于 `date` 的第 `n` 个交易日。
- `get_logical_trading_date()` 沿用逻辑交易日规则：`ts >= 21:00` 时参考下一自然日，再取不早于参考日的最近交易日。

返回示例：

```python
client.get_previous_trading_date("20260603")  # pd.Timestamp("2026-06-02")
client.get_next_trading_date("20260603")      # pd.Timestamp("2026-06-04")
client.get_logical_trading_date("2026-06-03 21:10:00")
```

#### `get_future_chain(...)`

签名：

```python
get_future_chain(
    instrument: list[str] | str,
    start_dt: str | datetime.date | datetime.datetime | pd.Timestamp,
    end_dt: str | datetime.date | datetime.datetime | pd.Timestamp,
    *,
    expect_df: bool = True,
) -> pd.DataFrame | list[dict] | dict[str, list[dict]]
```

参数：

| 参数         | 类型                                                     | 含义                                           |
| ------------ | -------------------------------------------------------- | ---------------------------------------------- |
| `instrument` | `str | list[str]`                                        | 品种代码，例如 `"AG"` 或 `["AG", "AU"]`        |
| `start_dt`   | `str | datetime.date | datetime.datetime | pd.Timestamp` | 起始交易日                                     |
| `end_dt`     | `str | datetime.date | datetime.datetime | pd.Timestamp` | 结束交易日                                     |
| `expect_df`  | `bool`                                                   | 默认 `True` 返回 DataFrame；`False` 返回旧结构 |

默认返回 DataFrame，一行一个日期链：

```python
df = client.get_future_chain(["AG", "AU"], "2026-06-01", "2026-06-30")
# columns: instrument, date, codes
```

`expect_df=False` 时保留旧结构：

```python
{
    "AG": [{"date": "2026-06-03", "codes": ["AG2606"]}],
    "AU": [{"date": "2026-06-03", "codes": ["AU2606"]}]
}
```

#### `get_option_chain(...)`

签名：

```python
get_option_chain(
    future_code: list[str] | str,
    start_dt: str | datetime.date | datetime.datetime | pd.Timestamp,
    end_dt: str | datetime.date | datetime.datetime | pd.Timestamp,
    *,
    option_type: str = "",
    expect_df: bool = True,
) -> pd.DataFrame | list[dict] | dict[str, list[dict]]
```

参数：

| 参数          | 类型                                                     | 含义                                                        |
| ------------- | -------------------------------------------------------- | ----------------------------------------------------------- |
| `future_code` | `str | list[str]`                                        | 标的期货合约代码，例如 `"AG2606"` 或 `["AG2606", "AU2606"]` |
| `start_dt`    | `str | datetime.date | datetime.datetime | pd.Timestamp` | 起始交易日                                                  |
| `end_dt`      | `str | datetime.date | datetime.datetime | pd.Timestamp` | 结束交易日                                                  |
| `option_type` | `str`                                                    | `""` 不过滤，`"C"` 看涨，`"P"` 看跌                         |
| `expect_df`   | `bool`                                                   | 默认 `True` 返回 DataFrame；`False` 返回旧结构              |

默认返回 DataFrame，一行一个日期链：

```python
df = client.get_option_chain(["AG2606", "AU2606"], "2026-06-01", "2026-06-30")
# columns: future_code, date, codes
```

`expect_df=False` 时保留旧结构：

```python
{
    "AG2606": [{"date": "2026-06-03", "codes": ["AG2606C8000", "AG2606P8000"]}]
}
```

### 2.3 行情监听

### 2.3.1 `start_quote_callbacks(...)`

签名：

```python
start_quote_callbacks(
    codes: list[str] | str,
    *,
    freq: str, 
    on_quote=None, 
    on_subscribe=None,
    on_error=None,
    on_message=None,
    daemon: bool = True,
) -> RemoteWebSocketSubscription
```

返回对象 `RemoteWebSocketSubscription`：

| 方法                 | 含义                 |
| -------------------- | -------------------- |
| `stop()`             | 请求停止后台 WS 监听 |
| `join(timeout=None)` | 等待后台线程退出     |
| `is_alive()`         | 后台线程是否仍在运行 |

`freq` 的合法值：

- `freq="tick"`：订阅 tick channel，内部 channel 为 `tick.CODE`。
- `freq="1m"`、`"5m"`、`"15m"`、`"30m"`、`"1h"`：订阅 bar channel，内部 channel 为 `bar.FREQ.CODE`。
- 不要传 `freq="bar"`。这会被拼成 `bar.bar.CODE`，服务端会返回 `UNSUPPORTED_FREQ`。

回调函数说明：

| 回调 | 触发时机 | 函数签名 | 入参含义 |
| ---- | -------- | -------- | -------- |
| `on_quote` | 只在收到 `tick` / `bar` 行情消息时触发 | `on_quote(data, raw)` | `data` 是行情主体，即 `raw["data"]`；`raw` 是完整 WebSocket 消息 |
| `on_subscribe` | 收到订阅回执时触发 | `on_subscribe(report)` | `report["contracts"]` 按 code 分组，包含合约存在性、is_live、订阅结果和该合约自己的 `session_state`；所有时间字段已转换为 `pd.Timestamp` |
| `on_message` | 收到任何 WS 消息都会触发 | `on_message(message)` | `message` 是转换后的完整 WebSocket 消息，所有时间字段已转换为 `pd.Timestamp`，包括 `connected`、`subscribed`、`tick`、`bar`、`error` 等 |
| `on_error` | 收到运行期 `type="error"` 的 WS 消息时触发 | `on_error(error)` | `error` 是 SDK 整理后的错误结构；合约不存在、非 live、非交易时段 idle 不触发 |

`raw` 的用途：

- 判断消息类型：`raw["type"]`，例如 `tick` / `bar`。
- 判断订阅通道：`raw["channel"]`，例如 `tick.AG2606` / `bar.1m.AG2606`。
- 判断事件：`raw["event"]`，例如 `update` / `closed`。
- 获取服务端时间：`raw["server_time"]`（`pd.Timestamp` 类型）。

WS 消息和回调触发规则：

| 消息形态 | 触发回调 | 含义 | 判断方式 |
| -------- | -------- | ---- | -------- |
| `{"type": "connected", ...}` | `on_message` | WS 已连接成功 | `message.get("type") == "connected"` |
| `{"type": "status", "code": "INGESTION_STOPPED", ...}` | `on_message` | 行情采集当前未运行，通常是非交易采集时段或 ingestion 已停止 | `message.get("type") == "status"` 且 `message.get("code") == "INGESTION_STOPPED"` |
| `{"op": "subscribed", "channels": [...], "contracts": {...}}` | `on_message` + `on_subscribe` | 订阅回执。`channels` 是实际订阅成功的 channel，`contracts` 按合约给出 `exists/is_live/subscribed/session_state/reason` | 查看 `report["contracts"][code]` |
| `{"type": "error", "code": "UNSUPPORTED_FREQ", ...}` | `on_message` + `on_error` | 周期不支持，例如误传 `freq="bar"` | `error.get("code") == "UNSUPPORTED_FREQ"` |
| `{"type": "error", "code": "INVALID_CHANNEL", ...}` | `on_message` + `on_error` | channel 格式不合法 | `error.get("code") == "INVALID_CHANNEL"` |
| `{"type": "tick", "event": "update", "channel": "tick.CODE", "data": {...}}` | `on_message` + `on_quote` | tick 更新 | `raw["type"] == "tick"` |
| `{"type": "bar", "event": "update" / "closed", "channel": "bar.1m.CODE", "data": {...}}` | `on_message` + `on_quote` | bar 更新或收盘冻结 | `raw["type"] == "bar"`；`raw["event"]` 区分 `update` / `closed` |
| `{"op": "unsubscribed", ...}` | `on_message` | 取消订阅回执 | `message.get("op") == "unsubscribed"` |
| `{"op": "pong", ...}` | `on_message` | ping 回包 | `message.get("op") == "pong"` |

重要边界：

- **时间格式统一**：SDK 对所有回调中的时间字段（包括 `on_quote` 的 `data`/`raw`、`on_subscribe` 的 `report["contracts"]`/`raw`、`on_error` 的 `raw`、`on_message` 的 `message`）统一转换为 `pd.Timestamp`。不再返回原始字符串。用户如需字符串格式，可使用 `str(ts)` 或 `ts.isoformat()`。
- 非交易采集时段或 ingestion stopped 时，WS 会返回 `INGESTION_STOPPED`；订阅回执中每个合约的 `session_state` 通常为 `idle`。
- `session_state` 是合约级别字段：不同品种交易时间不同，同一个订阅批次内可能同时出现 `active` 和 `idle`。
- 合约不存在不会再作为行情订阅错误推给 `on_error`；会出现在订阅回执 `contracts[code]` 中，`exists=False/subscribed=False/session_state="unknown"`。
- 如果需要立刻校验合约是否存在和是否 `is_live`，使用 `validate_contracts()` 或 REST `/api/v1/contracts/validate`。
- `get_latest_tick()` / `get_latest_ticks()` 遇到不存在合约返回 `quote_status="contract_not_found"`；`get_bar_window()` / `get_bar_windows()` 返回空表。
- `INGESTION_STOPPED` 是 `status` 消息，只触发 `on_message`，不会触发 `on_error`。

`daemon = True`时，脚本结束后，后台 WS 线程不会卡住整个 Python 进程。适合脚本、notebook、测试场景：主程序退出时不会被后台 WS 线程阻塞。

正式长期运行的策略程序可以设置 daemon=False，并在退出时显式调用：

`handle.stop()`
`handle.join(timeout=5)`

示例：

```python
def on_quote(data, raw):
    print(raw["type"], raw["channel"], data["code"], data.get("last_price"))

def on_message(message):
    msg_type = message.get("type")
    op = message.get("op")
    if msg_type == "status" and message.get("code") == "INGESTION_STOPPED":
        print("WS idle: ingestion stopped")
    elif op == "subscribed":
        print("WS subscribed:", message.get("channels"))
    else:
        print("WS message:", msg_type or op)

def on_subscribe(report):
    for code, item in report["contracts"].items():
        print(
            code,
            "exists=", item["exists"],
            "is_live=", item["is_live"],
            "subscribed=", item["subscribed"],
            "session_state=", item["session_state"],
            "reason=", item["reason"],
        )

def on_error(error):
    print("WS runtime error:", error)

sub = client.start_quote_callbacks(
    ["AG2606"],
    freq="tick",
    on_quote=on_quote,
    on_subscribe=on_subscribe,
    on_message=on_message,
    on_error=on_error,
)

# 主线程继续做别的事情...

sub.stop()
sub.join(timeout=3)
```

## 3. 原生 REST / Websocket 接口

**该接口优势在于可用于其他语言。这里先不暴露，后续如果有特殊需求，请联系我。**

## 4. 已知BUG / 未解决问题

#### **4.1 迟到/倒序tick**

由于迅投服务器不稳定，推送的tick会有偶发性的迟到问题。例如在 local_time = 10:55:42.000 时才收到 event_time = 10:54:58.000 的tick数据，这会导致tick的推送可能会倒序。

#### 4.2 bar推送的修正问题

由于上述的tick迟到问题，例如在10:55:00.000 label_dt = 10:55这根 1m bar 已经形成，此时仍然会保留30s的correction状态，如果在 local_time = 10:55:18.000时，来了一根 event_time = 10:54:58.000的tick数据，此时 label_dt = 10:55 这根 1m bar会被重新修正，bar_engine会再次推送一个 correction 状态的 label_dt = 10:55 的 1m bar。30s结束后，即local_time = 10:55:30.000 后，label_dt = 10:55 这根 1m bar 进入frozen状态，同时入CH库，不再更改。

迅投在服务器稳定时，一般推送延迟不会超过3s，但在特殊时期，甚至会出现超过1m的推送延迟。这有时候会导致出现一个巨大的tick真空期，此时的bar不可信。暂无解决方案。后续2.0版本通过对接CTP解决。

#### 4.3 断线问题

由于迅投服务器有时候会在盘中出现断连，对于本系统而言，造成的影响就是从开始断线开始，直到重连结束后的一段时间数据丢失。暂无解决方案。因此历史数据库不再采用实时对接的行情来进行注入，而是通过别的办法定时更新，时效性无法解决。

#### 4.4 其他问题

由于测试时间有限，有时候可能会出现一些其他情况，造成数据对不齐。目前观察而言没有发现这种情况。

#### 4.5 网络问题

不知道在局域网 / 公网内网穿透端会不会有压力
