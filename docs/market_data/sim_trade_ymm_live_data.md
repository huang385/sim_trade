# sim_trade接入YMM Live Data

## 运行依赖

交易程序使用客户端发行包`ymm-live-data-sdk==0.8.5`，导入模块为
`ymm_live_data_sdk`。该包由内部发布渠道提供，可使用内部索引安装：

```powershell
python -m pip install --index-url <内部Python索引地址> ymm-live-data-sdk==0.8.5
```

也可以把管理员发放的客户端wheel放在部署目录后使用相对路径安装：

```powershell
python -m pip install .\packages\ymm_live_data_sdk-0.8.5-py3-none-any.whl
```

`docs/reference/ymm_live_data_hub-0.5.6-py3-none-any.whl`是服务端Hub，要求
Python 3.12并包含FastAPI、PostgreSQL、ClickHouse和RQData服务依赖。它不是客户端，
不得安装进交易程序环境，也不得被交易代码导入。

## 配置

```env
REMOTE_MARKET_DATA_MODE=lan
REMOTE_MARKET_DATA_BASE_URL=
REMOTE_MARKET_DATA_API_TOKEN=
YMM_DATA_SDK_TOKEN=
REMOTE_MARKET_DATA_CA_FILE=
REMOTE_MARKET_DATA_VERIFY_SSL=true
```

`MODE`按管理员要求选择`lan`、`TS`或`local`。使用模式内置地址时无需填写
`BASE_URL`；管理员要求自定义WSS地址时再填写。Token不能为空，代码和日志不会
输出Token、完整连接地址或SDK会话信息。官方客户端不支持关闭TLS校验。

`YMM_DATA_SDK_TOKEN`属于`ymm_data_sdk`数据库客户端，与Live SDK Token分开
配置。它只在新增实际行情订阅时低频查询一次当前交易日最后Tick，不进行轮询。

## 字段映射

| YMM Live Data Tick | 内部MarketTick |
|---|---|
| `order_book_id` | `order_book_id`，再通过Instrument取得`exchange_id`和`symbol` |
| `trading_date` | `trading_day` |
| `datetime` | `event_time`，无时区时按Asia/Shanghai解释 |
| `last` / `prev_close` | `last_price` / `pre_close` |
| `open` / `high` / `low` | `open_price` / `high_price` / `low_price` |
| `volume` | `cumulative_volume` |
| `total_turnover` | `cumulative_turnover` |
| `open_interest` | `open_interest` |
| `bid[0]` / `bid_vol[0]` | `bid_price_1` / `bid_volume_1` |
| `ask[0]` / `ask_vol[0]` | `ask_price_1` / `ask_volume_1` |
| `event_id`或`source_event_id` | `source_event_id` |
| `sequence_id`或`sequence` | `sequence_id` |

源端没有事件编号时，对不含本地接收时间的稳定Tick业务字段做SHA-256；没有序号时
再从该稳定事件编号派生审计序号。普通实时阶段不按内容或时间丢弃Live Tick；只有
数据库初始化过渡期间会按`datetime`水位阻止旧Live Tick重复进入业务链。每条被接受
的行情都执行一次Redis Lua原子双写。Redis Stream的消息编号、Pending恢复和
成交结算幂等仍负责消息重投安全，两者不能与行情内容去重混为一谈。

需要特别说明：本次任务说明称源端已经完成Tick去重，但当前官方客户端指南明确写着
“tick不做去重”，两份约束并不一致。交易程序按更保守的边界实现：不依赖源端去重，
也不在接收端丢弃内容相同的合法回调；是否启用源端去重应由行情服务方最终确认。

## 订阅、重连和停止

一个Worker只创建一个`LiveMarketDataClient`。启动顺序是状态消费者、行情消费者、
批量订阅；活动订单和活动持仓合约变化后，通过公开`subscribe()`/`unsubscribe()`
批量增订和退订。SDK负责心跳、网络重连及重连后的订阅恢复，项目不再启动第二套
网络重连循环。Worker停止时调用`client.close()`并等待行情、状态线程退出。

新状态键为：

```text
market:source:ymm_live_data:status
```

旧行情源代码、文档和Redis状态键已在真实连接、真实Tick及下游链路验收完成后删除。
系统不存在新源失败后自动回退旧源的运行分支。

## 新增订阅的初始化行情

新增合约订阅时，Worker先完成Live SDK订阅，再异步调用
`ymm_data_sdk.get_price(..., frequency="tick")`补取当前交易日最后一条已入库Tick。
数据库查询不会阻塞Live SDK回调，两类结果进入同一个本地消费队列：

1. 数据库结果返回前已有时间相同或更新的Live Tick时，数据库结果丢弃；
2. 暂无Live Tick或数据库Tick更新时，数据库Tick以`YMM_DATA_SDK / REST_SNAPSHOT`
   身份进入现有Redis Stream、WebSocket、PnL和撮合链；
3. 数据库Tick成为初始化水位后，时间不晚于该水位的Live Tick不重复处理；
4. 第一条时间更新的Live Tick到达后恢复原有实时处理，数据库初始化水位失效；
5. 同一实际订阅不会重复查询；退订后重新新增时可以重新初始化。

若数据库行情恰逢发布窗口并返回临时`running`不可用状态，单次查询先在
`REMOTE_MARKET_DATA_TIMEOUT_SECONDS`内短暂重试；仍失败时，只对该活动订阅按
`MARKET_DATABASE_SNAPSHOT_RETRY_SECONDS`（默认15秒）低频退避。Live SDK订阅与
实时行情处理在此期间保持正常，成功或退订后立即停止重试。

数据库Tick没有官方事件编号，系统对稳定业务字段生成带`BOOTSTRAP-`前缀的
事件ID。成交表原有`order_id + market_event_id`唯一约束继续作为最终幂等防线。
数据库SDK查询日期与返回Tick的`trading_date`不一致时不会接入。
