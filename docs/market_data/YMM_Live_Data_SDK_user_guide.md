# YMM FeedHub API Python SDK 用户指南

## 快速开始

```python
import ymm_live_data_sdk
from ymm_live_data_sdk import LiveMarketDataClient

ymm_live_data_sdk.init(
    token="管理员发放的策略 Token",
    mode="lan",  # Tailscale 用户使用 "TS"
)

client = LiveMarketDataClient()


def handle(message):
    enqueue_for_strategy(message)  # 必须快速、非阻塞


def handle_status(event):
    print(event.component, event.state, event.message, event.details)


feed_thread = client.listen(handle)          # 非阻塞行情回调线程
status_thread = client.listen_status(handle_status)
client.subscribe([
    "tick_000001.XSHE",
    "bar_000001.XSHE",       # 标准1m频道
    "bar_000001.XSHE_5m",    # 5m
    "bar_AU2612_60m",        # 60m
])

try:
    run_strategy_main_loop()
finally:
    client.close()
```

`client.listen(handler)`会启动后台回调线程。也可以使用阻塞迭代器：

```python
try:
    for message in client.listen():
        enqueue_for_strategy(message)
finally:
    client.close()
```

收盘或低流动性合约长时间没有回调是正常现象。`listen()`等待消息时不会持续占用一个CPU核。
应先启动行情和状态消费者，再提交订阅，避免行情已经开始到达而策略尚未消费。

## SDK接口参考

### 包级初始化

```python
import ymm_live_data_sdk

ymm_live_data_sdk.init(
    token="管理员发放的策略Token",
    mode="lan",
)
```

`ymm_live_data_sdk.init(token, mode=None, server_url=None, ca_file=None)`保存后续客户端的默认配置，
它本身不建立连接。`mode`支持`"lan"`、`"TS"`和仅限服务器本机的`"local"`。普通
用户应使用管理员指定的模式，通常不需要手动填写`server_url`或`ca_file`。

`ymm_live_data_sdk.close()`只会清除上述包级默认配置，不会关闭已经创建的客户端。
关闭实际行情连接必须调用`client.close()`。

### `LiveMarketDataClient`构造器

```python
client = LiveMarketDataClient(
    token=None,
    mode=None,
    server_url=None,
    ca_file=None,
)
```

构造器会立即建立WSS连接、完成鉴权，并取得初始`info`和`status`。不传参数时使用
`ymm_live_data_sdk.init()`保存的默认配置；也可在构造器中覆盖配置，但每个客户端
仍必须使用自己的策略Token。

鉴权失败、根CA或地址错误、连接超时会直接从构造器抛出异常，不会返回一个“未连接的
客户端”。

### 客户端的全部公开方法和属性

| 接口 | 返回值 | 用途与注意事项 |
|---|---|---|
| `client.subscribe(channels)` | `None` | 订阅一个字符串或一组字符串。重复订阅幂等；无效频道以Python warning拒绝。 |
| `client.unsubscribe(channels)` | `None` | 取消一个或一组频道。取消未订阅的频道也是幂等操作。 |
| `client.listen()` | 阻塞迭代器 | 用`for message in client.listen()`在当前线程持续取行情。 |
| `client.listen(handler)` | `threading.Thread` | 启动一个后台行情回调线程，每条行情调用一次`handler(message)`。 |
| `client.listen_status()` | 阻塞迭代器 | 在当前线程持续取`StatusEvent`。 |
| `client.listen_status(handler)` | `threading.Thread` | 启动一个后台状态回调线程，在状态变化时调用`handler(event)`。 |
| `client.get_status()` | `CenterStatus` | 主动向中心请求一份新快照，适合人工诊断或偶尔校验；严禁高频轮询。 |
| `client.close()` | `None` | 关闭WSS和SDK后台线程，结束行情/状态迭代器。重复调用安全。 |
| `client.subscriptions` | `list[str]` | 当前策略希望保持的频道，按字符串排序。 |
| `client.info` | `dict` | 当前鉴权会话的版本和身份信息；返回副本，修改它不会修改客户端。 |
| `client.status` | `CenterStatus` | SDK本地保存的最新状态快照。读取属性不会发送网络请求。 |

对一个客户端，行情流和状态流各建立一个主要消费者即可。多次调用`listen()`不会把每条
行情广播给多个handler，而是多个消费者竞争同一队列。

如果连接正处于短暂断线期，`get_status()`无法向中心发起请求，会返回当前本地缓存的
快照。策略不应用高频`get_status()`代替`listen_status()`。

`client.info`包含：

| 字段 | 含义 |
|---|---|
| `server_version` | 当前FeedHub服务端版本。 |
| `protocol_version` | WSS协议版本，当前为1。 |
| `user` | Token登记的用户。 |
| `strategy` | Token绑定的策略。 |
| `session_id` | 本次连接的唯一会话ID；断线重连后可能变化。 |

SDK公开异常的共同基类是`YMMMessageHubError`，并细分为配置错误、鉴权错误、连接错误、
协议错误和同Token新连接接管旧连接的`YMMMessageHubReplacedError`。

## `client.status`字段与状态含义

`client.status`是不发起网络请求的最新本地快照：创建客户端时由握手返回，此后在收到中心
状态事件时更新。它是`CenterStatus`不可变数据类，要用属性而非字典下标访问：

```python
status = client.status
print(status.hub, status.rqdata, status.catalog, status.aggregation)
```

由于中心不会为每个计数器变化都发送事件，`client.status`中的吞吐、队列和落库计数可能是
上一次状态事件时的值。需要当前诊断数值时，手动调用一次`client.get_status()`；判断连接和
慢消费仍以`listen_status()`事件为主。

### 策略最需要关注的字段

| 字段 | 正常值 | 准确含义 |
|---|---|---|
| `hub` | `connected` | 当前SDK到FeedHub的连接及中心分发管线状态。 |
| `rqdata` | `connected` | FeedHub到RQData八个分片的聚合状态。 |
| `catalog` | `ready` | 当前交易日的全市场合约目录和交易时段已加载。 |
| `aggregation` | `connected` | 本地5m～240m合成管线正常。只用tick/1m的策略也应监听它，但高周期策略尤其关键。 |
| `storage` | `connected` | ClickHouse实时落库状态。它异常不会阻断行情实时推送。 |
| `dropped_messages` | `0` | 当前会话在服务端下行队列中累计丢弃的行情数。只要大于0，本次会话就已有缺口。 |
| `last_market_data_at` | 非空时间 | 中心最近一次向任一订阅会话分发行情的UTC时间；不代表当前策略的某个低流动性合约一定有tick。 |
| `authenticated_as` | Token所属用户 | 本会话鉴权为哪个用户。 |
| `strategy` | Token所属策略 | 本会话绑定的策略标签。 |
| `session_id` | 非空ID | 本次连接的会话ID。 |
| `critical_stop_requested` | `False` | `True`表示中心因元数据、配额或上游完整性等关键错误正在停止。 |
| `critical_stop_reason` | `None` | 关键停止的脱敏原因。 |

`dropped_messages`快照主要记录服务端下行队列的丢弃。SDK本机接收队列溢出时也会发送
`session/slow_consumer`事件，其`details["dropped_messages"]`才是当时最直接的依据。因此不能只偶尔
读`client.status.dropped_messages`，必须持续消费`listen_status()`。

### 目录、全市场订阅和证书字段

| 字段 | 含义 |
|---|---|
| `server_version` | FeedHub服务端版本。 |
| `certificate_not_after` / `certificate_days_remaining` | WSS服务器证书的UTC到期时间和剩余整天数。 |
| `catalog_trading_date` | 当前目录对应的逻辑交易日；夜盘通常已是下一交易日。 |
| `catalog_instrument_count` | 当前目录中的有效合约数。 |
| `catalog_updated_at` | 本次PG目录快照中的更新时间指纹。 |
| `catalog_metadata_state` | 当前元数据快照状态，正常为`ready`。 |
| `catalog_metadata_source` | 元数据来源，当前为`market_meta_RQ`。 |
| `catalog_metadata_target_date` | 本次元数据加载的目标交易日。 |
| `catalog_metadata_fetched_at` | FeedHub从PG完成读取的时间。 |
| `catalog_metadata_source_instrument_count` | PG返回的当日有效合约数。 |
| `catalog_metadata_covered_instrument_count` | 已成功匹配路由和交易时段的合约数。 |
| `catalog_missing_trading_period_count` | 缺失1m交易时段的合约数，正常必须为0。 |
| `catalog_metadata_duration_ms` | 本次PG目录加载耗时，毫秒。 |
| `catalog_metadata_request_count` | 本服务进程已执行的PG目录加载次数。 |
| `catalog_metadata_last_error` | 最近一次元数据错误；正常为`None`。 |
| `catalog_last_refresh_kind` | 最近一次加载类型：`startup`、`day`或`night`。 |
| `catalog_next_refresh_at` | 下一次计划的PG目录加载时间。 |
| `target_channel_count` / `confirmed_channel_count` / `failed_channel_count` | 中心当前RQData上游目标、已确认和失败频道数；是全局数，不是当前策略的订阅数。 |
| `subscription_synced` | 上游目标集合是否已全部确认。 |
| `baseline_target_channel_count` / `baseline_confirmed_channel_count` / `baseline_failed_channel_count` | 固定全市场tick+1m基线的目标、已确认和失败频道数。 |
| `baseline_subscription_synced` | 八分片是否已完整确认全市场基线；正常必须为`True`。 |

当前用户订阅只决定FeedHub向该会话转发哪些频道，不改变RQData全市场上游集合。
因此，查当前策略自己订了什么应使用`client.subscriptions`，不应使用上述channel count。

### 上游、合成和存储诊断字段

以下字段是整个FeedHub服务进程的运维统计，不是当前Token或当前策略的统计。普通策略
不需要根据它们自己计算报警阈值，但在中心异常时可一并提供给管理员。计数器通常从当前
服务进程启动后累计。

| 字段 | 含义 |
|---|---|
| `rqdata_active_shards` / `rqdata_total_shards` | 已连接的RQData分片数/总分片数，正常为`8/8`。 |
| `rqdata_bytes_used` / `rqdata_bytes_limit` | 两张行情License的已用/总字节配额汇总。 |
| `rqdata_quota_updated_at` | 最近一次RQData配额查询时间。 |
| `upstream_received_message_count` | RQData WebSocket层接收的消息数，可包含控制消息。 |
| `upstream_received_feed_count` / `upstream_received_bytes` | 上游解析出的行情条数/原始帧字节数。 |
| `upstream_receive_queue` / `upstream_receive_queue_high_watermark` / `upstream_receive_queue_limit` | 八分片队列当前待处理数之和、单分片进程期间峰值中的最大值、单分片配置容量。 |
| `upstream_receive_queue_paused` | `True`表示上游接收曾因内部背压暂停读取。 |
| `upstream_ipc_delivered_feed_count` / `upstream_ipc_dropped_feed_count` / `upstream_ipc_pending_feed_count` | License子进程已交付、已丢弃、待交付给主进程的行情数。 |
| `hub_received_feed_count` | Hub主进程已接收的行情数。 |
| `hub_ingress_queue` / `hub_ingress_dropped` | 存在下游订阅者时，Hub分发队列当前值/累计丢弃数。 |
| `aggregation_received_1m_rows` / `aggregation_accepted_1m_rows` | 合成管线收到/去重后接受的1m bar数。 |
| `aggregation_duplicate_bar_rows` / `aggregation_conflicting_bar_rows` / `aggregation_stale_bar_rows` | 完全重复、同键内容冲突、时间水位之前的1m bar数，这些均不再向下游发送。 |
| `aggregation_dropped_1m_rows` / `aggregation_pending_1m_rows` | 合成入口已丢弃/正在排队或处理的1m bar数。 |
| `aggregation_emitted_5m_rows` / `aggregation_emitted_15m_rows` / `aggregation_emitted_30m_rows` / `aggregation_emitted_60m_rows` / `aggregation_emitted_120m_rows` / `aggregation_emitted_240m_rows` | 各高周期已生成的闭合bar数。 |
| `aggregation_invalidated_bucket_count` | 启动、断线、目录替换或缺口后被废弃的未闭合高周期桶数。 |
| `aggregation_fanout_rows` | 对存在订阅者的bar频道已触发的分发次数；不是当前策略条数，也不是接收会话数之和。 |
| `aggregation_last_bar_at` | 合成管线最近处理有效bar的UTC时间。 |
| `aggregation_queue` / `aggregation_queue_high_watermark` / `aggregation_queue_limit` | 合成队列当前值、进程期间峰值和容量。 |
| `storage_ingress_queue` / `storage_batch_queue` | ClickHouse映射入口待处理数和已封批待写入批次数。 |
| `storage_last_write_at` | 最近一次ClickHouse成功写入时间。 |
| `storage_received_feed_count` | 存储管线收到的tick和已接受1m总数。 |
| `storage_inserted_tick_rows` / `storage_inserted_1m_rows` / `storage_inserted_higher_bar_rows` | 已成功写入的tick、1m和高周期行数。 |
| `storage_dropped_rows` / `storage_dropped_batches` / `storage_rejected_rows` | 存储管线已丢弃行、已丢弃批次和字段/类型校验拒绝行数。 |
| `pipeline_accounting_ok` | 上游、IPC、Hub、合成和存储的计数在当前快照是否守恒；是中心级诊断值。 |
| `cleanup_last_run_at` / `cleanup_cutoff_trading_date` | 实时库分区清理任务最近运行时间/清理截止交易日。 |

### 快照中各组件状态的含义

| 组件 | 可见状态 | 含义 |
|---|---|---|
| `hub` | `connected` | SDK连接和中心分发正常。 |
| `hub` | `disconnected` / `reconnecting` | SDK当前已断开/正自动重连FeedHub。断线期间不补行情。 |
| `hub` | `degraded` | 中心级分发队列发生过溢出，已存在行情缺口。 |
| `rqdata` | `connected` | 八个RQData分片全部连接。 |
| `rqdata` | `partial` | 仅部分分片连接，全市场行情不完整。 |
| `rqdata` | `reconnecting` / `disconnected` | RQData上游正重连/已全部断开。 |
| `rqdata` | `quota_exceeded` | RQData License配额超限，属于关键停机故障。 |
| `catalog` | `loading` | 正在等待或读取PG元数据。 |
| `catalog` | `ready` | 全市场目录、路由和交易时段完整可用。 |
| `catalog` | `error` / `disabled` | 目录校验失败/目录功能被禁用；常态服务不应继续产生信号。 |
| `aggregation` | `starting` | 高周期引擎未完成目录配置。 |
| `aggregation` | `connected` | 1m去重和5m～240m合成正常。 |
| `aggregation` | `degraded` | 合成入口丢失1m，受影响未闭合桶已作废。 |
| `storage` | `disabled` | 实时落库功能未启用；行情推送仍可正常。 |
| `storage` | `starting` | ClickHouse writer正在启动。 |
| `storage` | `connected` | 各存储写入通道正常且当前交易日未被标记不完整。 |
| `storage` | `degraded` / `failed` | 部分存储通道异常或当日存在落库缺口/写入发生不可恢复错误。实时推送不因此停止。 |

### `StatusEvent`的组件、事件状态和建议动作

`listen_status()`返回的是状态“变化事件”，不是定时快照。每个`StatusEvent`包含：

| 属性 | 含义 |
|---|---|
| `component` | 发生变化的组件：`hub`、`rqdata`、`catalog`、`aggregation`、`storage`或`session`。 |
| `state` | 本次变化类型，例如`reconnecting`、`recovered`或`slow_consumer`。 |
| `timestamp` | 事件UTC时间，ISO 8601字符串。 |
| `message` | 面向人的简短说明。 |
| `details` | 结构化补充信息，例如丢弃数、分片ID、关闭原因。未说明的字段不应作为策略业务协议。 |
| `sequence` | 中心为事件分配的序号；SDK本地事件使用本地序列。 |

常见事件及策略处理：

| 事件 | 含义 | 用户应对 |
|---|---|---|
| `hub/disconnected` | SDK与FeedHub连接断开，或中心即将停止。 | 立即暂停依赖实时行情的新信号，记录缺口。 |
| `hub/reconnecting` | SDK正按退避时间自动重连。 | 无需手工反复创建客户端；继续保持状态消费线程。 |
| `hub/recovered` | WSS已重连并重放本策略订阅，或中心分发队列已恢复。 | 只表示从现在起恢复；断线缺口未补发。 |
| `rqdata/disconnected` / `rqdata/reconnecting` | FeedHub的RQData上游不完整。 | 将行情标记为不连续，暂停对完整性有要求的新信号。 |
| `rqdata/recovered` | RQData上游恢复并重放全市场基线。 | 不补断线期间数据；需要连续窗口的指标应重新预热。 |
| `rqdata/quota_exceeded` | 上游License配额超限，中心将关键停止。 | 立即停止交易信号并通知管理员。 |
| `catalog/reconnecting` | 启动落在08:30～08:45或20:30～20:45维护窗口，正等待计划加载。 | 等待`catalog/recovered`，不在目录未就绪时产生信号。 |
| `catalog/recovered` | PG目录快照成功加载或切换。 | 等待中心其他核心状态也恢复后使用。 |
| `catalog/disconnected` | PG加载或目录结构校验失败，中心将关键停止。 | 停止信号并通知管理员。 |
| `aggregation/slow_consumer` | 中心高周期合成入口溢出，部分1m已丢失。 | 使用5m～240m的策略将窗口标记为不完整；这不是当前用户handler过慢。 |
| `aggregation/recovered` | 合成队列排空且持续无新丢失。 | 新的完整桶可继续使用，之前缺口不会补齐。 |
| `hub/slow_consumer` | 中心共享分发队列溢出，已发生中心级丢失。 | 所有受影响策略都应记录缺口；这不等于当前用户handler过慢。 |
| `session/slow_consumer` | 当前会话服务端下行队列，或当前SDK本机队列溢出；最旧行情已被丢弃。 | 这才是当前策略慢消费的直接告警。立即暂停信号、检查handler和重新预热。 |
| `session/replaced` | 同Token的新连接已接管，旧客户端随后关闭且不再自动抢回连接。 | 检查是否重复启动了策略或在Notebook中遗留旧客户端。 |
| `session/disconnected` 且`details.reason == "token_revoked"` | Token已被管理员撤销，SDK停止。 | 不要重试旧Token，联系管理员。 |
| `storage/storage_degraded` / `storage/schema_mismatch` / `storage/slow_consumer` | ClickHouse落库失败、字段不匹配或存储队列压力。 | 实时回调可以继续；不要把它误判为当前策略慢消费，但应将信息转告管理员。 |
| `storage/recovered` | ClickHouse存储通道恢复。 | 只表示后续可落库，之前丢弃的存储数据不补写。 |

`recovered`是一个“变化事件”，对应的快照状态通常已回到`connected`或`ready`。不要等待
`client.status.hub == "recovered"`这样的条件。

## 订阅频道

支持以下频道：

- `tick_<order_book_id>`：RQData逐笔快照；
- `bar_<order_book_id>`：RQData闭合1m bar；
- `bar_<order_book_id>_5m/15m/30m/60m/120m/240m`：FeedHub生成的闭合高周期bar；
- `bar_<order_book_id>_1m`：1m兼容别名，订阅时会产生warning，建议使用标准写法`bar_<order_book_id>`。

订阅只从成功订阅之后开始推送，不补发此前行情。目录外、已经退出或拼写错误的合约会产生
Python warning并被拒绝。

重复订阅同一频道是幂等操作。`bar_<id>`和`bar_<id>_1m`是两个请求频道；如果同时订阅，用户会
按各自的`channel`分别收到数据，因此不建议同时使用。

## 回调行情示例

行情回调是RQData风格的Python字典，不添加FeedHub专用字段。下面价格和数量仅为格式示意；不同
资产类别、交易所及交易阶段的可选字段可能不同，策略不应假设每条消息都包含全部示例字段。

典型tick：

```python
{
    "action": "feed",
    "channel": "tick_000001.XSHE",
    "order_book_id": "000001.XSHE",
    "datetime": datetime.datetime(2026, 8, 3, 9, 30, 0, 123000),
    "trading_date": datetime.date(2026, 8, 3),
    "last": 10.52,
    "prev_close": 10.48,
    "open": 10.50,
    "volume": 128400,
    "total_turnover": 1350840.0,
    "ask": [10.53, 10.54, 10.55, 10.56, 10.57],
    "ask_vol": [1200, 2600, 1800, 3500, 2100],
    "bid": [10.52, 10.51, 10.50, 10.49, 10.48],
    "bid_vol": [900, 1700, 3100, 2400, 4600],
}
```

期货或期权tick还可能包含`prev_settlement`、`open_interest`、`limit_up`和`limit_down`等字段。
低流动性合约只有在盘口或成交发生变化时才可能出现新tick。

典型闭合1m bar：

```python
{
    "action": "feed",
    "channel": "bar_000001.XSHE",
    "order_book_id": "000001.XSHE",
    "datetime": datetime.datetime(2026, 8, 3, 9, 31),
    "trading_date": datetime.date(2026, 8, 3),
    "open": 10.50,
    "high": 10.55,
    "low": 10.49,
    "close": 10.52,
    "volume": 128400,
    "total_turnover": 1350840.0,
    "num_trades": 936,
}
```

典型闭合5m bar：

```python
{
    "action": "feed",
    "channel": "bar_000001.XSHE_5m",
    "order_book_id": "000001.XSHE",
    "datetime": datetime.datetime(2026, 8, 3, 9, 35),  # 右标签
    "trading_date": datetime.date(2026, 8, 3),
    "open": 10.50,             # 桶内首根1m的open
    "high": 10.61,             # 桶内最高值
    "low": 10.49,              # 桶内最低值
    "close": 10.58,            # 桶内末根1m的close
    "volume": 586200,          # 桶内求和
    "total_turnover": 6179380.0,
    "num_trades": 4218,
}
```

高周期只在完整桶闭合后推送，不发送正在形成的bar，也不事后修正。若同一时点同时闭合多个周期，
顺序为1m、5m、15m、30m、60m、120m、240m。策略读取可选字段时建议使用
`message.get("field")`并明确处理`None`或字段缺失。

tick不做去重：即使两条tick的时间戳及内容完全相同，也会逐条回调。bar必须去重；同一合约、
周期、交易日和时间只采用首条有效bar。

## 慢消费

“慢消费”是指行情进入客户端的速度持续快于策略处理速度。例如在行情回调中逐条打印、写数据库、
发网络请求、执行复杂计算或持有锁，都可能造成消息积压。订阅合约越多，策略需要具备的持续处理能力
和开盘瞬间处理能力越高。

行情缓冲区是有限的，不能作为长期积压区。缓冲区满时会丢弃最旧行情、保留较新的行情；连接通常
继续保持，也不会通过行情回调抛出异常。被丢弃的行情不会自动补发。

用户应始终监听状态流。发生慢消费时会收到类似事件：

```python
StatusEvent(
    component="session",
    state="slow_consumer",
    message="outbound queue overflowed; oldest market-data messages were dropped",
    details={"dropped_messages": 37},
)
```

如果是SDK本机接收缓冲区溢出，`message`会是：

```text
local SDK feed queue overflowed; oldest messages were dropped
```

`details["dropped_messages"]`是累计丢弃数；通知会限频，因此不会为每一条丢失行情各发一次事件。
只有`component == "session"`表示当前用户会话或本机SDK消费过慢；其他组件的状态事件不应误判为
自己的行情处理函数过慢。

生产策略应遵循以下要求：

1. 行情回调只做极轻量操作；简单增量计算可以直接完成，耗时任务应交给其他线程后立即返回。
2. 磁盘写入、数据库访问和网络请求不要直接放在行情回调中执行。
3. 不要对大量tick逐条`print`或同步写日志；应使用采样日志和聚合指标。
4. 使用用户自己的工作队列时，必须同时启动真正消费该队列的工作线程，并处理队列满的情况。
5. 收到`session/slow_consumer`后，将策略标记为数据不完整并立即告警；对不能容忍缺口的计算，清空
   受影响的未完成状态，从下一个完整周期重新开始，或通过独立历史数据源恢复。不要把缺口静默当作
   连续行情继续计算。
6. 只订阅策略实际需要的频道。订阅量较大时，必须先验证策略能够及时处理开盘峰值行情。

### `work_queue`是什么

`work_queue`不是FeedHub的慢消费阈值，也不会由FeedHub监控。它是用户程序可选的“任务交接队列”：
行情回调把消息快速放入其中，用户自己的工作线程再从中取出消息执行耗时计算。

只有回调中的业务处理比较耗时时才需要`work_queue`。如果只是像文末MA示例一样，对少量1m bar做
简单增量计算，可以直接在回调中完成，不需要再建一层队列。

如果用户自己的`work_queue`满了，说明策略处理速度已经跟不上输入速度。此时应由用户程序记录缺口
并告警；它与FeedHub通过`session/slow_consumer`发出的告警是两套不同的检查。

## 策略接入的推荐结构

### 先规划订阅集合

策略应把订阅集合当作配置的一部分，在启动时根据交易标的和所需周期一次性生成。推荐做法是：

- 只需要分钟或高周期指标时，直接订阅对应bar，不要同时订阅无用途的tick；
- 只有确实依赖成交、盘口或盘中最新价时才订阅tick；
- 将多个频道作为一个列表传给一次`subscribe()`，不要在循环中反复提交相同订阅；
- 运行中更换标的池时，计算新旧集合的`added/removed`，分别调用一次`subscribe()`和
  `unsubscribe()`；
- 使用`client.subscriptions`检查当前策略希望保持的订阅集合，但不要高频轮询或重复订阅；
- 在开盘前启动并完成订阅；盘中临时订阅只会收到之后的行情，不能获得指标所需的历史窗口。

不同合约的行情不应被理解为一条具有全局先后关系的序列。策略只应维护自己真正需要的顺序约束，
例如同一合约的增量盘口和bar状态；跨合约比较应使用行情时间、交易日和策略定义的对齐规则。

### 一个策略只建立一个行情入口

一个策略进程应只创建一个`LiveMarketDataClient`，并通过这个客户端订阅该策略需要的全部频道。
策略内部的风控、信号、展示和记录模块如果需要共享同一份行情，应由策略进程在内部分发，而不是用
同一Token重复创建客户端。

每个客户端只应选择一种行情消费方式：

- 使用一次`client.listen(handler)`；或者
- 使用一次`for message in client.listen()`。

不要对同一个客户端调用多次`listen()`。多个消费者会竞争同一个接收队列，每个消费者只能拿到
部分消息，它不是向多个处理函数广播。状态流同样只应建立一个主要消费者，再由策略内部转发需要的
状态。

推荐启动顺序为：

1. 初始化策略状态；
2. 启动`listen_status()`状态回调；
3. 启动`listen()`行情回调；
4. 一次性订阅所需频道；
5. 确认初始状态正常后，再允许策略产生交易信号。

推荐关闭顺序为：先禁止产生新信号，再调用`client.close()`停止接收，最后按策略要求处理或丢弃
本地尚未消费的队列。`unsubscribe()`适合运行期间动态减少频道，但程序退出前不要求逐个退订。

### 行情回调只负责接收和路由

SDK的行情handler是串行调用的。前一条消息的handler没有返回，后一条消息就只能等待。因此，
handler的目标是快速完成必要的轻量处理并返回。简单增量指标可以直接计算；较重的业务处理再交给
用户自己的工作线程。

handler内适合执行：

- 读取`channel`和`order_book_id`；
- 更新少量计数器或简单的增量指标；
- 如果业务处理较重，使用`put_nowait()`把消息交给用户自己的工作队列。

handler内不应执行：

- `time.sleep()`或等待锁、条件变量；
- HTTP、数据库、Redis及其他网络请求；
- 同步写文件、逐条刷日志或逐条`print`；
- 下单并同步等待委托响应；
- 对每条tick重新构造完整`DataFrame`、全量重算指标或执行模型推理；
- 无界重试，或在异常处理中执行耗时恢复。

传入的`message`应当视为只读对象。把它交给多个模块时不要原地修改；需要添加策略字段时创建自己的
包装对象，避免一个模块的修改影响另一个模块。

回调函数抛出的未捕获异常会结束行情handler线程。此后网络连接可能仍然存在，但无人消费本地行情，
最终就会出现`slow_consumer`。策略必须监控`feed_thread.is_alive()`，不能让线程静默退出。

## 用户侧监控与告警

状态的推荐使用方式是“初始快照+后续回调”：

1. `LiveMarketDataClient()`连接成功后，通过`client.status`读取服务器在握手时返回的初始状态；
2. 随后使用`client.listen_status(handler)`接收状态变化回调；
3. `client.get_status()`是主动查询接口，适合人工诊断或偶尔校验，不需要策略每隔几秒轮询；
4. 用户程序另外检查`feed_thread`和`status_thread`是否仍然存活。

正常状态应满足：`hub/rqdata/aggregation == "connected"`、`catalog == "ready"`，没有收到
`session/slow_consumer`，并且初始快照中的`dropped_messages == 0`。

只要出现一次`slow_consumer`或`dropped_messages > 0`，就说明当前会话已经缺少行情，不能因为
队列后来恢复就把数据当作连续。实盘策略应暂停依赖该行情的新信号并告警；重新完成指标预热或重启
策略后才能恢复。`recovered`只表示连接恢复，不表示缺失行情已经补发。

低流动性合约没有盘口或成交变化时可能长期不产生tick，因此不能用“某个合约多久没tick”单独判断
连接异常，应以状态接口和状态事件为主。

## 高周期口径及与行情软件的差异

FeedHub的5m～240m按RQData历史分钟线口径合成，使用交易分钟和右标签。它不是把每次开盘后的
连续行情简单地每N分钟切一刀。

对于交易时段不能被周期整除、包含午休或夜盘跨日的品种，短尾桶、周期标签和每日bar数量可能与
部分行情软件不同。商品期货及商品期权的60m等周期还采用RQData的时钟切片规则。因此，行情软件中
看到的AU合约60m标签序列不一定与FeedHub一致，这不表示漏了一根bar。

策略验收和回测应以`ymm_data_sdk`/`market_data_RQ`中的RQData历史bar为对照，不应以其他行情
软件的K线标签作为唯一标准。FeedHub只用收到的完整1m生成高周期；盘中启动、断线或1m缺口影响的
不完整桶会被跳过，且不会补发。

## Token、策略与连接

Token与策略一一绑定：一个Token代表一个明确的策略实例，并且同一时间只允许一个活动连接。

- 一个`LiveMarketDataClient`连接可以订阅多个合约和多个周期，无需为每个频道创建客户端。
- 同一个客户端可以同时运行一个行情监听线程和一个状态监听线程；“单连接”不等于只能有一个
  Python线程。
- 同一台机器运行多个独立策略时，每个策略必须申请不同Token。
- 不要在多个进程、脚本或Notebook中共享同一Token，也不要用生产Token进行临时测试。
- 同一Token建立新连接后，新连接会接管，旧连接先收到`session/replaced`，随后关闭并停止自动
  重连。主动重启策略时应先调用旧客户端的`close()`。
- Token还会绑定获准的LAN来源IP或Tailscale身份；更换来源后应联系管理员更新绑定。

普通网络中断时SDK会自动重连并恢复该策略的订阅，但断线期间的行情不补发。可通过
`client.listen_status()`监听`disconnected`、`reconnecting`和`recovered`，通过
`client.get_status()`主动获取当前中心状态。

## 示例：多合约1m MA5/MA10金叉死叉

下面是一个只做信号演示的完整示例。它同时订阅多个合约的1m bar，分别计算MA5和MA10。
示例先使用`client.status`读取初始状态，之后只通过`listen_status()`接收状态变化，不循环查询中心。
只有中心状态正常、消费线程存活并且当前会话从未丢失行情时，才会输出金叉/死叉信号。

```python
# deque只保留最近10个收盘价，适合计算MA5和MA10。
from collections import deque
import threading
import time

import ymm_live_data_sdk
from ymm_live_data_sdk import LiveMarketDataClient


# 一个客户端可以同时订阅多个合约；请替换成策略实际使用的合约。
ORDER_BOOK_IDS = [
    "000001.XSHE",
    "600000.XSHG",
    "AU2612",
]

# 初始化SDK。LAN用户使用"lan"，Tailscale用户使用"TS"。
# 一个Token只属于一个策略，并且同一时间只能建立一个连接。
ymm_live_data_sdk.init(
    token="管理员发放的策略Token",
    mode="lan",  # Tailscale用户使用"TS"
)

# 创建客户端时会立即建立WSS连接，并取得一次初始中心状态。
client = LiveMarketDataClient()

# 每个合约分别保存最近10根1m bar的close。
closes = {order_book_id: deque(maxlen=10) for order_book_id in ORDER_BOOK_IDS}

# 保存每个合约上一根bar计算出的MA5-MA10，用来判断是否刚刚穿越。
previous_diff = {}

# 线程安全的状态标志：是否慢消费、是否有数据缺口、是否允许产生信号。
slow_consumer_seen = threading.Event()
data_gap = threading.Event()
market_data_ready = threading.Event()

# 四个中心组件的正常状态。storage不影响用户实时推送，因此不作为信号开关。
GOOD_STATES = {
    "hub": "connected",
    "rqdata": "connected",
    "catalog": "ready",
    "aggregation": "connected",
}

# client.status是WSS握手时已经返回的初始快照，不会再次查询服务器。
initial_status = client.status
component_states = {
    "hub": initial_status.hub,
    "rqdata": initial_status.rqdata,
    "catalog": initial_status.catalog,
    "aggregation": initial_status.aggregation,
}

# 如果初始快照已经记录过丢弃，则这个会话不能直接用于产生信号。
if initial_status.dropped_messages > 0:
    slow_consumer_seen.set()
    data_gap.set()

# 随后会把SDK返回的行情线程和状态线程保存在这里。
feed_thread = None
status_thread = None


def refresh_readiness():
    """根据本地保存的状态更新信号开关，不向中心发起查询。"""
    threads_alive = (
        feed_thread is not None
        and status_thread is not None
        and feed_thread.is_alive()
        and status_thread.is_alive()
    )
    center_ok = all(
        component_states[name] == expected
        for name, expected in GOOD_STATES.items()
    )
    normal = center_ok and threads_alive and not data_gap.is_set()

    if normal:
        market_data_ready.set()
    else:
        market_data_ready.clear()

    # 状态变化频率很低，可以打印；不要在行情回调中逐条打印行情。
    print(
        "HEALTH",
        f"normal={normal}",
        f"slow_consumer={slow_consumer_seen.is_set()}",
        f"threads_alive={threads_alive}",
        f"states={component_states}",
    )


def on_bar(message):
    """每收到一根闭合1m bar，执行一次很轻量的增量MA计算。"""
    # 状态不正常时直接忽略行情，不产生新的模拟/实盘信号。
    if not market_data_ready.is_set():
        return

    # 从回调字典中取得合约和收盘价，并忽略不属于本示例的数据。
    order_book_id = message.get("order_book_id")
    close = message.get("close")
    if order_book_id not in closes or close is None:
        return

    # deque加入第11个值时会自动删除最旧值，因此始终最多保存10个close。
    values = closes[order_book_id]
    values.append(float(close))

    # MA10至少需要10根bar；不足10根时只积累数据，不计算信号。
    if len(values) < 10:
        return

    # MA5使用最近5个close，MA10使用deque中的全部10个close。
    ma5 = sum(list(values)[-5:]) / 5
    ma10 = sum(values) / 10
    diff = ma5 - ma10

    # old_diff是上一根bar的MA差值，diff是当前bar的MA差值。
    old_diff = previous_diff.get(order_book_id)
    previous_diff[order_book_id] = diff

    # MA5从下方向上穿越MA10：金叉。
    if old_diff is not None and old_diff <= 0 < diff:
        print(
            message["datetime"],
            order_book_id,
            "金叉",
            f"MA5={ma5:.4f}",
            f"MA10={ma10:.4f}",
        )

    # MA5从上方向下穿越MA10：死叉。
    elif old_diff is not None and old_diff >= 0 > diff:
        print(
            message["datetime"],
            order_book_id,
            "死叉",
            f"MA5={ma5:.4f}",
            f"MA10={ma10:.4f}",
        )


def on_status(event):
    """中心状态变化时由SDK主动回调，不需要用户轮询。"""
    # details中会包含dropped_messages等补充信息。
    print("STATUS", event.component, event.state, event.message, event.details)

    # 更新本地保存的组件状态。recovered表示恢复到该组件的正常状态。
    if event.component in component_states:
        component_states[event.component] = (
            GOOD_STATES[event.component]
            if event.state == "recovered"
            else event.state
        )

    # session/slow_consumer表示服务端会话或SDK本地缓冲已经丢过行情。
    slow = event.component == "session" and event.state == "slow_consumer"

    # 同一Token建立了新连接时，旧连接会收到session/replaced并停止。
    replaced = event.component == "session" and event.state == "replaced"

    # 这些状态表示核心行情链路当前不可连续使用。
    broken = (
        slow
        or replaced
        or (
            event.component in {"hub", "rqdata", "catalog", "aggregation"}
            and event.state in {
                "disconnected",
                "reconnecting",
                "partial",
                "quota_exceeded",
                "degraded",
                "slow_consumer",
                "error",
            }
        )
    )
    if slow:
        slow_consumer_seen.set()
    if broken:
        # data_gap一旦设置便不自动清除，因为recovered不会补发缺失行情。
        data_gap.set()

    # 每次状态变化后，使用本地状态重新计算是否允许产生信号。
    refresh_readiness()


# 先启动状态回调，再启动行情回调，避免错过早期状态变化。
status_thread = client.listen_status(on_status)
feed_thread = client.listen(on_bar)

# 两个线程都已启动，现在根据初始快照设置第一次健康状态。
refresh_readiness()

# 一次订阅多个合约的标准1m频道。订阅本身不会创建多个WSS连接。
client.subscribe([f"bar_{order_book_id}" for order_book_id in ORDER_BOOK_IDS])

try:
    # 主线程只负责保持程序存活，并检查两个回调线程是否意外退出。
    while True:
        if not feed_thread.is_alive() or not status_thread.is_alive():
            data_gap.set()
            market_data_ready.clear()
            print("HEALTH normal=False：行情或状态回调线程已经退出")
            break
        time.sleep(1)
except KeyboardInterrupt:
    # 用户按Ctrl+C时进入finally，正常关闭客户端。
    print("正在停止策略……")
finally:
    # close()会关闭WSS连接并结束SDK线程；重复调用也是安全的。
    client.close()
```

正常启动时会看到一次`HEALTH normal=True`。之后仅在中心状态发生变化时再次输出状态，不会定时
查询中心。出现`normal=False`、`slow_consumer=True`或回调线程退出时，不应继续使用当前MA状态产生
实盘信号。

这个示例启动后会等待每个合约收到10根新1m bar才开始判断交叉。正式策略如果需要启动后立即得到
MA信号，应先从历史数据源预加载至少10根1m bar，再与实时bar按交易日和`datetime`衔接去重。
发生断线或慢消费后，示例会一直保持不可用；应重新完成历史预热或重启策略，而不能仅因收到
`recovered`就继续使用旧MA窗口。
