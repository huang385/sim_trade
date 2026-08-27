---
title: YMM FeedHub API Python SDK用户指南
status: current
owner: YMM FeedHub API
last_reviewed: 2026-08-26
applies_to: Live Data SDK 0.8.5 / Windows LAN users
source_of_truth: true
---

# YMM FeedHub API Python SDK用户指南

本指南面向在Windows电脑上运行Python策略的用户，介绍如何安装SDK、连接FeedHub、订阅行情和
处理常见问题。

SDK 0.8.5使用Tick/Bar双消费链：策略分别通过`tick_handler`和`bar_handler`接收1～512个行情
字典组成的tuple。两个handler由独立线程并发调用，Bar峰值不会在SDK中排在Tick前面。中心和SDK均
不会等待批次凑满。

## 0.8.5 LAN测试分发包

管理员向每位LAN测试人员提供以下两个文件：

```text
ymm_live_data_sdk-0.8.5-py3-none-any.whl
user-guide.md（本指南）
```

正式0.8.5 wheel的SHA-256固定为：

```text
a0aca5fb4bd0461a732891a1149c8303c1dbd95472405d636d3da8cfbf670d25
```

普通用户wheel只包含行情客户端、内嵌根CA和默认关闭的本地Canary；不会安装Windows Service、
WinSW、Data SDK，不会开放端口或修改防火墙/注册表。每位测试人员必须使用管理员分配给自己的
唯一Live Token；不要共享`.153`管理员Canary Token，也不要在两个进程或Notebook中共用Token。

从0.8.4升级到0.8.5不改变订阅、双handler、迭代器或行情字典接口。0.8.5修复了Canary瞬时失败
恢复后的stale状态；普通行情策略可以直接使用`pip install --upgrade`升级。

## 1. 使用前准备

开始前请确认：

- Windows电脑已经安装Python 3.10或更高版本；
- 已从管理员处取得`ymm_live_data_sdk-0.8.5-py3-none-any.whl`；
- 已取得当前策略专用的Live Token；
- 管理员已告知使用LAN还是Tailscale模式；
- 使用Tailscale时，客户端已经登录并显示连接正常。

一个Token只供一个策略连接。使用已占用Token启动第二个程序时，新程序会报错，原策略连接不会中断。

## 2. 安装SDK

在策略目录打开PowerShell，先检查Python版本：

```powershell
py --version
```

版本应为3.10或更高。然后创建独立虚拟环境并安装管理员提供的wheel：

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade "C:\path\to\ymm_live_data_sdk-0.8.5-py3-none-any.whl"
.\.venv\Scripts\python.exe -c "import ymm_live_data_sdk as y; print(y.__version__)"
```

把示例wheel路径替换为实际文件位置。最后一条命令应输出：

```text
0.8.5
```

安装前必须校验SHA-256：

```powershell
$wheel = "C:\path\to\ymm_live_data_sdk-0.8.5-py3-none-any.whl"
$expected = "a0aca5fb4bd0461a732891a1149c8303c1dbd95472405d636d3da8cfbf670d25"
$actual = (Get-FileHash $wheel -Algorithm SHA256).Hash.ToLower()
if ($actual -ne $expected) {
    throw "SDK wheel SHA256不匹配：$actual"
}
Write-Output "SDK_WHEEL_VERIFIED sha256=$actual"
```

这些命令不需要“以管理员身份运行”。必须使用管理员提供的wheel；它已经包含连接所需证书。

正常用户只需传`token`和`mode`。`server_url`用于管理员指定非默认WSS地址，`ca_file`用于管理员
指定另一份受信任根CA；传`None`时分别使用LAN/TS默认地址和wheel内置CA。不要通过这两个参数
关闭TLS验证。

## 3. 保存Token

不要把Token直接写进Python源码、PowerShell命令、Git、Notebook输出、日志或聊天记录。建议把它
保存在当前Windows用户目录下的独立文件中。

在PowerShell中执行：

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.ymm" | Out-Null
notepad "$env:USERPROFILE\.ymm\feedhub.token"
```

在记事本中只写一行管理员提供的Live Token，不要添加引号或其他文字，然后保存。不要把该文件放在
共享目录、网盘同步目录或代码仓库中。

## 4. 快速连接

在策略目录新建`feedhub_quickstart.py`，复制以下内容。示例同时验证Tick和Bar两条独立消费链，
但每条lane只打印第一次回调，避免测试程序本身因大量`print`成为慢消费者：

```python
from pathlib import Path
from threading import Event

import ymm_live_data_sdk
from ymm_live_data_sdk import LiveMarketDataClient, YMMMessageHubTokenInUseError


TOKEN_FILE = Path.home() / ".ymm" / "feedhub.token"
MODE = "lan"  # Tailscale用户改为"TS"
ORDER_BOOK_ID = "000001.XSHE"
CHANNELS = [
    f"tick_{ORDER_BOOK_ID}",
    f"bar_{ORDER_BOOK_ID}",
]
tick_seen = Event()
bar_seen = Event()


def on_ticks(batch):
    if not tick_seen.is_set():
        message = batch[-1]
        print(
            "TICK_CALLBACK",
            f"rows={len(batch)}",
            message.get("channel"),
            message.get("datetime"),
        )
        tick_seen.set()


def on_bars(batch):
    if not bar_seen.is_set():
        message = batch[-1]
        print(
            "BAR_CALLBACK",
            f"rows={len(batch)}",
            message.get("channel"),
            message.get("datetime"),
        )
        bar_seen.set()


def on_status(event):
    print("STATUS", event.component, event.state, event.message)


token = TOKEN_FILE.read_text(encoding="utf-8").strip()
ymm_live_data_sdk.init(token=token, mode=MODE)

client = None
try:
    client = LiveMarketDataClient()
    print(
        "CONNECTED",
        f"sdk={ymm_live_data_sdk.__version__}",
        f"user={client.info['user']}",
        f"strategy={client.info['strategy']}",
    )

    client.subscribe(CHANNELS)
    status_thread = client.listen_status(on_status)

    print("SUBSCRIBED", client.subscriptions)
    print(
        "CENTER",
        client.status.hub,
        client.status.rqdata,
        client.status.catalog,
        client.status.aggregation,
    )

    # listen()阻塞在当前线程；两个handler由两个SDK线程并发调用。
    client.listen(tick_handler=on_ticks, bar_handler=on_bars)
    print("行情消费已经退出")
except KeyboardInterrupt:
    print("正在停止……")
except YMMMessageHubTokenInUseError as exc:
    print("当前Token正在使用中，原连接未被中断：")
    print(exc.session.strategy, exc.session.computer_name, exc.session.process_id)
finally:
    if client is not None:
        client.close()
    ymm_live_data_sdk.close()
```

在PowerShell中运行：

```powershell
.\.venv\Scripts\python.exe .\feedhub_quickstart.py
```

连接成功后应先看到类似输出：

```text
CONNECTED sdk=0.8.5 user=... strategy=...
SUBSCRIBED ['bar_000001.XSHE', 'tick_000001.XSHE']
CENTER connected connected ready connected
```

连续竞价期间通常先看到`TICK_CALLBACK`；只有产生新的闭合1分钟bar后才会看到`BAR_CALLBACK`。
休市、集合竞价、分钟尚未闭合或低流动性合约暂时没有某类回调都属于正常情况，不能仅凭零样本判定
断流。按`Ctrl+C`结束程序，正常关闭后该Token Session应被释放。

## 5. 订阅频道

支持的标准频道如下：

| 频道格式 | 内容 |
|---|---|
| `tick_<order_book_id>` | RQData tick快照 |
| `bar_<order_book_id>` | 闭合1分钟bar |
| `bar_<order_book_id>_5m` | 闭合5分钟bar |
| `bar_<order_book_id>_15m` | 闭合15分钟bar |
| `bar_<order_book_id>_30m` | 闭合30分钟bar |
| `bar_<order_book_id>_60m` | 闭合60分钟bar |
| `bar_<order_book_id>_120m` | 闭合120分钟bar |
| `bar_<order_book_id>_240m` | 闭合240分钟bar |

标准1分钟频道是`bar_<order_book_id>`。不要使用`bar_<order_book_id>_1m`兼容写法，SDK会给出
warning。

一次可以订阅多个频道：

```python
client.subscribe(
    [
        "tick_000001.XSHE",
        "bar_000001.XSHE",
        "bar_000001.XSHE_5m",
        "bar_600000.XSHG_60m",
    ]
)
```

运行中可以增加或取消频道：

```python
client.subscribe("bar_600000.XSHG")
client.unsubscribe("tick_000001.XSHE")
print(client.subscriptions)
```

无效、过期或拼写错误的频道会产生Python warning并被拒绝。订阅只接收成功订阅之后的新行情，
不会补发之前的数据。

## 6. 消费行情

### 双回调方式

```python
def handle_ticks(batch):
    tick_work_queue.put_nowait(batch)


def handle_bars(batch):
    bar_work_queue.put_nowait(batch)


client.listen(tick_handler=handle_ticks, bar_handler=handle_bars)
```

`listen()`启动两个独立handler线程，并在调用它的线程中阻塞至客户端关闭。任一
handler的未捕获异常会关闭两条连接，并由`listen()`重新抛出。

### 独立阻塞迭代方式

```python
for batch in client.iter_ticks():
    process_tick_batch(batch)

# 另一个线程可独立消费Bar：
for batch in client.iter_bars():
    process_bar_batch(batch)
```

batch始终是包含1～512个行情字典的tuple。Tick lane只有Tick；Bar lane中raw 1m与
higher按服务端实际接纳顺序FIFO，并可能出现在同一次batch。服务端只合并当前已经就绪的逻辑帧，
不会为凑满batch增加等待。两条lane不建立跨lane顺序。每个lane只能有一个
handler或迭代器消费者。开始消费时，已订阅lane必须有对应消费者；消费开始后不能
再订阅一条没有消费者的lane。

SDK交付后不会再修改或复用这个tuple及其dict；只要策略把它们当作只读数据，可直接将
batch引用放入线程安全内存队列，无需深拷贝。

### 行情字典

常用字段包括：

| 字段 | 含义 |
|---|---|
| `channel` | 当前频道 |
| `order_book_id` | 合约代码 |
| `datetime` | 行情时间 |
| `trading_date` | 逻辑交易日 |
| `open/high/low/close` | bar价格字段 |
| `volume/total_turnover` | 成交量和成交额 |
| `last/ask/bid` | tick常见字段 |

不同资产和交易阶段可能缺少某些字段，应使用`message.get("field")`并处理`None`。夜盘自然日期可能
与`trading_date`不同，策略分组时应同时使用行情时间与交易日；任何策略级去重还必须遵循下述
Tick、原始1m和higher各自不同的重复语义。

Tick是行情快照，不是逐笔成交或逐笔委托数据；时间戳或内容相同的重复tick仍会逐条保留，策略不得
仅按时间戳去重。标准`bar_<id>`是RQData已经闭合的原始1分钟bar；上游回调多少条，中心就转发多少
条，不对原始1分钟bar去重。本地5m～240m只在计划桶闭合后发布，不推送正在形成的bar；正常时桶
完整，只有已确认技术故障窗口才允许发布使用实际收到分钟计算的非空partial。

partial higher的行情字典与普通higher完全相同，不增加`partial/bar_status/observed_count`等字段。
中心会另外发送`aggregation/partial_bar`状态事件，并在`event.details["quality_dimensions"]`中给出
受影响的交易日、资产和频率。忽略状态消费者的策略无法仅从单根行情判断它是否partial。

以下是为了说明结构而编写的示例，数值不是实际行情。RQData可能按资产类别省略不适用字段。

```python
# 代表性的tick回调
{
    "action": "feed",
    "channel": "tick_AU2612",
    "order_book_id": "AU2612",
    "datetime": datetime(...),
    "trading_date": date(...),
    "last": 500.0,
    "volume": 12345,
    "total_turnover": 67890.0,
    "open_interest": 45678,
    "ask": [500.1, 500.2, ...],
    "bid": [499.9, 499.8, ...],
    "ask_vol": [10, 20, ...],
    "bid_vol": [12, 18, ...],
}

# 代表性的闭合1分钟bar回调
{
    "action": "feed",
    "channel": "bar_AU2612",
    "order_book_id": "AU2612",
    "datetime": datetime(...),
    "trading_date": date(...),
    "open": 499.8,
    "high": 500.2,
    "low": 499.7,
    "close": 500.0,
    "volume": 100,
    "total_turnover": 50000.0,
    "open_interest": 45678,
}
```

## 7. 监听状态

策略应同时运行一个状态消费者：

```python
def handle_status(event):
    print(event.component, event.state, event.message)


status_thread = client.listen_status(handle_status)
```

正常使用原始tick和1m时，主要中心快照应为：

```python
status = client.status
raw_realtime_ready = (
    status.hub == "connected"
    and status.rqdata == "connected"
    and status.catalog == "ready"
)

# 只有使用本地5m～240m时，才额外要求Aggregation当前可用。
higher_ready = raw_realtime_ready and status.aggregation == "connected"
```

`client.status`适合查看最近快照，`client.get_status()`适合偶尔主动查询；两者都不能替代
`listen_status()`。SDK本机行情队列溢出会首先产生`session/slow_consumer`事件，策略必须持续消费
状态回调并记录自己是否见过该事件。

用户最需要处理的状态如下：

| 状态 | 应对方式 |
|---|---|
| `hub/disconnected`或`hub/reconnecting` | 暂停依赖连续行情的新信号，等待自动重连 |
| `hub/recovered` | 连接已经恢复，但断线期间的数据不会补发 |
| `session/slow_consumer` | 当前策略已经丢失行情，立即停止相关信号并重新预热 |
| `session/closed` | 当前连接已被Token持有者手动关闭；确认后再新建客户端 |
| Token被撤销 | 不要继续重试，联系管理员 |
| `catalog`不是`ready` | 暂停新信号，等待恢复或联系管理员 |
| `aggregation`不是`connected` | 高周期行情可能不完整，暂停相关信号 |
| `aggregation/partial_bar` | 只暂停或告警`quality_dimensions`列出的交易日、资产和频率；行情字典本身不带partial标记 |

如果收到`storage`相关异常但行情回调仍在继续，可以继续接收实时行情，同时把状态信息交给管理员。

`recovered`只代表之后恢复正常，不代表缺失行情已经补齐。对行情连续性有要求的策略应重新加载历史
窗口后再恢复信号。

## 8. 避免慢消费

SDK分别把Tick和Bar帧放入本机固定的2048行有界FIFO，再由对应handler线程按批调用。
网络层Tick物理包最多512条，Bar物理包最多2048条；SDK始终逐逻辑frame校验并重新组成1～512条
handler批次，所以用户代码不会收到2048条tuple。
两条队列不互借容量；任一lane长期处理过慢时，SDK只整帧丢弃该lane最旧行情并发出
带`lane`的`session/slow_consumer`。这表示该lane数据已经出现缺口，不是“快要丢失”的预警。

行情回调必须快速返回。以下操作不要直接放在回调中：

- 网络或数据库请求；
- 同步写文件；
- 大量逐条`print`；
- 长时间计算、模型推理或等待锁；
- 下单后同步等待结果。

处理较重时，应把消息快速放入策略自己的工作队列，由其他线程处理：

```python
from queue import Full, Queue


work_queue = Queue(maxsize=10000)
strategy_queue_overflowed = False


def handle(batch):
    global strategy_queue_overflowed
    try:
        work_queue.put_nowait(batch)
    except Full:
        strategy_queue_overflowed = True
        alert_strategy_queue_overflow()
```

`work_queue`是策略自己的缓冲区，用于让SDK回调立即返回；它不是FeedHub的慢消费阈值。队列满表示
策略工作线程同样跟不上，策略应停止生成新信号并重新预热，而不是静默丢弃后继续交易。还应保留
调用`listen()`的主线程，让handler异常向上抛出，避免异常被静默吞掉。

## 9. MA5/MA10示例

下面的完整示例订阅一个合约的闭合1分钟bar，用工作线程计算MA5/MA10，并通过状态回调监控连接和
慢消费。示例只打印信号，不包含下单逻辑。

```python
import time
from collections import deque
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Thread

import ymm_live_data_sdk
from ymm_live_data_sdk import LiveMarketDataClient


CHANNEL = "bar_000001.XSHE"
TOKEN_FILE = Path.home() / ".ymm" / "feedhub.token"

# 策略工作队列：行情回调只负责入队，指标计算放在另一个线程。
work_queue = Queue(maxsize=2000)
stop = Event()
market_data_unsafe = Event()


def on_market_data(batch):
    """由SDK行情线程调用；必须快速返回。"""
    try:
        work_queue.put_nowait(batch)
    except Full:
        # 这是策略自己的队列溢出，同样意味着已经丢失连续性。
        market_data_unsafe.set()
        print("ERROR: strategy work queue overflowed; stop using new signals")


def on_status(event):
    """由独立状态线程调用；不要用行情回调代替状态监控。"""
    print("STATUS", event.component, event.state, event.message)
    if event.component == "session" and event.state == "slow_consumer":
        # SDK本机队列已经丢弃旧行情，不能继续假设MA窗口连续。
        market_data_unsafe.set()
    elif event.component in {"hub", "rqdata"} and event.state in {
        "disconnected",
        "reconnecting",
    }:
        # FeedHub不补发断线期间行情，需要重新加载历史窗口后才能恢复信号。
        market_data_unsafe.set()
    # 本例只订阅RQData原始1m；Aggregation是异步higher旁路，故障不会
    # 阻塞或污染这个原始1m订阅。使用5m～240m的策略应另外检查
    # aggregation状态及partial_bar的quality_dimensions。


def calculate_ma():
    """策略工作线程：按策略自己的first-valid分钟槽计算MA。"""
    closes = deque(maxlen=10)
    seen_slots = set()
    seen_slot_order = deque()
    previous_relation = None

    while not stop.is_set():
        try:
            batch = work_queue.get(timeout=1)
        except Empty:
            continue

        try:
            for message in batch:
                close = message.get("close")
                if close is None:
                    continue
                # FeedHub会原样转发上游重复1m。这个MA示例只需要每分钟一个值，
                # 因而在策略派生视图中采用first-valid；这不是SDK去重规则。
                slot = (
                    message.get("order_book_id"),
                    message.get("trading_date"),
                    message.get("datetime"),
                )
                if slot in seen_slots:
                    continue
                seen_slots.add(slot)
                seen_slot_order.append(slot)
                if len(seen_slot_order) > 64:
                    seen_slots.discard(seen_slot_order.popleft())
                closes.append(float(close))
                if len(closes) < 10:
                    continue

                values = list(closes)
                ma5 = sum(values[-5:]) / 5
                ma10 = sum(values) / 10
                relation = ma5 > ma10

                # 数据连续性已经失效时仍可计算指标，但禁止据此产生新交易信号。
                if not market_data_unsafe.is_set() and previous_relation is not None:
                    if relation and not previous_relation:
                        print("SIGNAL: MA5 crossed above MA10", message["datetime"])
                    elif not relation and previous_relation:
                        print("SIGNAL: MA5 crossed below MA10", message["datetime"])
                previous_relation = relation
        finally:
            work_queue.task_done()


token = TOKEN_FILE.read_text(encoding="utf-8").strip()
ymm_live_data_sdk.init(token=token, mode="lan")
client = LiveMarketDataClient()

worker = Thread(target=calculate_ma, name="ma-worker", daemon=True)
worker.start()
client.subscribe(CHANNEL)
status_thread = client.listen_status(on_status)

try:
    client.listen(bar_handler=on_market_data)
finally:
    stop.set()
    client.close()
    ymm_live_data_sdk.close()
    worker.join(timeout=2)
```

如果发生断线、SDK慢消费或策略工作队列溢出，示例会永久设置`market_data_unsafe`，不会仅因连接
恢复就自动重新交易。真实策略应通过YMM Data SDK重新加载MA窗口、完成衔接校验后，再由自己的
恢复逻辑清除该状态。

## 10. 历史预热与断线恢复

FeedHub只推送订阅之后的新行情。需要MA、波动率或其他历史窗口的策略，应先通过YMM Data SDK的
`get_price()`加载历史数据，再接入FeedHub实时流。

推荐顺序：

1. 加载截至衔接点的历史窗口；
2. 启动状态和行情消费者；
3. 提交实时订阅；
4. 按行情类型处理衔接边界；
5. 发生断线或慢消费后重新加载受影响窗口。

第4步不能写成一条适用于所有行情的时间戳去重规则：

- Tick重复具有业务意义；恢复后从第一条新tick继续，不补发故障期间数据，也不按datetime删除重复tick。
- 原始1m同样会保留上游重复。如果策略指标只需要每分钟一个值，应由策略自己的派生视图采用
  first-valid分钟槽，就像上面的MA示例；原始行情计数和审计仍保留全部回调。
- 5m～240m由中心按first-valid发布。把历史窗口接到实时higher时，可以用
  `channel + order_book_id + trading_date + datetime`避免同一根已发布higher在策略窗口中重复计入。
- 收到`slow_consumer`、断线或`partial_bar`后，是否恢复交易必须同时参考状态事件和重新加载的数据，
  不能只看最后一个时间戳。

高周期bar采用RQData时间切片规则，标签或每天的bar数量可能与其他行情软件不同。策略验收和回测
应使用YMM Data SDK中的RQData历史bar作为对照。

## 11. Token和连接

- 一个Token对应一个策略和一个逻辑Session；SDK自动管理Tick/Bar两条物理连接；
- 一个客户端可以同时订阅多个合约和周期；
- 多个独立策略必须申请不同Token；
- 不要在多个进程或Notebook中共享Token；
- Token已被占用时，新连接会报错，原连接继续运行；
- 普通网络中断由SDK自动重连并恢复订阅；
- 程序退出前应调用`client.close()`。

`client.close()`是明确结束逻辑Session：它先使用当前精确Session ID安全释放服务端Token占用，再关闭
本地Tick/Bar线程。正常返回后无需再等待网络断线超时；普通临时断线仍保留Session并自动重连。若关闭
时服务端不可达，SDK会在2秒内完成本地退出并给出warning，此时再使用下方安全Session API核对。

### 查询当前占用

调用查询方法不会建立行情连接，也不会影响正在运行的策略：

```python
session = ymm_live_data_sdk.get_token_session()

if session is None:
    print("当前Token未被占用")
else:
    print("策略：", session.strategy)
    print("电脑：", session.computer_name)
    print("进程ID：", session.process_id)
    print("连接时间：", session.connected_at)
```

### 手动关闭占用

只有在确认需要停止该连接时，才使用刚查询到的`session_id`关闭：

```python
session = ymm_live_data_sdk.get_token_session()
if session is not None:
    closed = ymm_live_data_sdk.close_token_session(session.session_id)
    print("已关闭" if closed else "该连接已经结束")
```

关闭的是FeedHub行情连接，不会结束对方的Windows程序。对方SDK会停止自动重连；需要恢复时，用户应
重新创建`LiveMarketDataClient`。

LAN和Tailscale Token的接入方式不同。更换电脑、LAN地址或Tailscale账号后，应先联系管理员更新
登记信息。

## 12. 本地Canary延迟诊断

SDK 0.8.5包含默认关闭的本地Canary。它只统计当前客户端实际收到的行情，不改变订阅、不上传结果、
不开启Exporter，也不会出现在管理员Grafana。普通wheel不会注册Windows Service、安装WinSW或
Data SDK、开放端口或修改防火墙；只有管理员`.153`专用Bundle具有常驻服务。

附着到已经运行的客户端：

```python
canary = client.start_canary(
    duration_seconds=600,
    interval_seconds=15,
    output="console",
)

# 原有subscribe/listen代码保持不变。
client.subscribe(["tick_AU2612", "bar_AU2612", "bar_AU2612_5m"])
client.listen(tick_handler=handle_ticks, bar_handler=handle_bars)

# listen结束后仍持有handle时，可以读取或保存报告。
report = canary.snapshot()
canary.write_json("canary-report.json")
```

默认运行10分钟，最长30分钟；同一客户端只能运行一个普通Canary。每15秒报告Tick、raw 1m和
higher的样本数、p50/p95/p99/max，并分开显示：

- FeedHub数据面入口→WSS frame ready；
- WSS frame ready→本机socket收到；
- socket→解码→handler开始；
- handler执行时间。

跨机器指标需要时钟校准。`trusted`表示不确定性不超过1ms，`warning`为1～5ms；超过5ms时报告
不发布跨机器分位数，但仍保留同机SDK阶段和连接事实。

需要在策略之外测试显式频道时：

```powershell
$env:YMM_LIVE_DATA_TOKEN = (Get-Content "$HOME\.ymm\feedhub.token" -Raw).Trim()
ymm-live-data-canary probe `
  --channels-file .\canary-channels.txt `
  --duration-seconds 600 `
  --json-out .\canary-report.json
```

频道文件可为逐行文本或JSON字符串列表，最多512个频道。独立probe会建立一个新逻辑Session；如果
策略正在使用同一个Token，会按正式规则得到`YMMMessageHubTokenInUseError`。此时应使用附着API或
另一个专用测试Token，不能关闭正在运行的策略Session来规避。

Canary报告中的`WSS frame ready→socket`包含服务端Session排队、TLS/WebSocket发送、LAN和本机
socket调度，不是纯网络耗时。普通用户报告只保存在本机；联系管理员时可提供脱敏统计，不要提交完整
Token、原始行情或策略业务数据。

需要与管理员LAN测试机`.153`做同名单、同窗口的自动对比时，不准备频道文件：

```powershell
ymm-live-data-canary compare `
  --duration-seconds 600 `
  --json-out .\canary-comparison.json
```

SDK会从FeedHub取得当前唯一的64合约/192频道Roster，完成时钟校准后从下一个服务端15秒边界开始。
默认连续测试10分钟，允许60～1800秒且必须是15秒整数倍。测试结束后只下载`.153`同时间段的聚合
Baseline，在本机输出每15秒、每分钟及完整周期p50/p95/p99/max、差值和倍数；不能通过平均窗口
p95计算完整周期p95。

只有Roster revision、窗口、数量、样本指纹、sequence及双方时钟全部一致时才比较延迟。普通用户
结果不会上传或进入Grafana；正在运行的策略不能共享Token，并行测试应使用管理员提供的临时Canary
Token。

## 13. Windows常见问题

| 现象 | 处理方法 |
|---|---|
| PowerShell提示找不到`py` | 安装Python 3.10或更高版本，并重新打开PowerShell |
| `No module named ymm_live_data_sdk` | 确认使用的是`.\.venv\Scripts\python.exe`，并重新安装wheel |
| 提示缺少受信任证书 | 安装了错误的wheel；重新使用管理员提供的生产wheel |
| 连接超时或TLS失败 | 检查LAN/Tailscale模式、Windows系统时间和网络连接；仍失败时联系管理员 |
| 鉴权失败 | 检查是否误用了Data SDK Token，以及当前电脑/账号是否与登记信息一致 |
| 提示`YMMMessageHubTokenInUseError` | 查看异常中的策略、电脑和进程ID；原连接仍在运行，确认后再手动关闭 |
| 已连接但没有行情 | 检查频道warning、交易时段和分钟是否闭合；休市或静默合约没有行情是正常的 |
| 收到`session/slow_consumer` | 减少订阅量，把耗时处理移出回调，并重新预热策略窗口 |
| `listen()`抛出异常 | 查看是Tick还是Bar handler抛错；SDK会关闭整个逻辑客户端 |

联系管理员时可以提供以下脱敏信息：

- SDK版本；
- LAN或Tailscale模式；
- `client.info`中的用户和策略名称；
- `client.status`；
- 完整异常类型和错误文字。

LAN测试人员还应按下面的固定格式反馈，便于与服务器和`.153`同时间段指标核对：

```text
测试开始/结束时间（Asia/Shanghai）：
Windows版本与Python版本：
SDK版本（必须为0.8.5）：
登记的user/strategy名称：
CONNECTED是否成功：
Tick首次回调时间与累计行数：
Bar首次回调时间与累计行数：
是否出现TokenInUse/slow_consumer/sequence gap/重连：
client.status中的hub/rqdata/catalog/aggregation：
若运行Canary compare：退出码和脱敏汇总：
```

不要提供完整Token、密码或大量原始行情内容。

## 14. 常用接口速查

| 接口 | 用途 |
|---|---|
| `ymm_live_data_sdk.init(token, mode)` | 保存连接配置 |
| `ymm_live_data_sdk.get_token_session()` | 查询当前Token是否有连接，不占用Token |
| `ymm_live_data_sdk.close_token_session(session_id)` | 关闭刚查询到的当前连接 |
| `LiveMarketDataClient()` | 建立连接 |
| `client.subscribe(channels)` | 增加订阅 |
| `client.unsubscribe(channels)` | 取消订阅 |
| `client.listen(tick_handler=..., bar_handler=...)` | 并发消费两条lane并阻塞至关闭 |
| `client.iter_ticks()` | 独占迭代Tick批次 |
| `client.iter_bars()` | 独占迭代raw 1m及higher批次 |
| `client.listen_status(handler)` | 启动状态回调线程 |
| `client.delivery_metrics` | 查看本地批大小、解码、排队、handler和丢弃指标 |
| `client.start_canary(...)` | 临时统计当前客户端的代表性交付延迟，不改变订阅或上传数据 |
| `client.subscriptions` | 查看当前订阅 |
| `client.status` | 查看最近状态 |
| `client.get_status()` | 主动获取一次状态，不要高频调用 |
| `client.close()` | 关闭连接 |

更完整的接口签名见[SDK接口速查](sdk-api-reference.md)。
