# 客户端行情 WebSocket

## 职责边界

`/ws/market` 只发布公司服务端已标准化的行情，不发布订单、成交、持仓、
资金或 PnL。交易事实继续由 `/ws/trading` 发布。客户端不得连接 Redis、
内部 Stream、Feed SDK，也不会取得上游行情账号或 Token。

行情来源仍是交易服务使用的同一上游数据：

```text
上游行情 SDK -> MarketDataSubscriberWorker -> 标准 MarketTick
              -> 最新行情 Hash + 行情 Stream -> /ws/market
```

## 连接认证

客户端使用内存中的 Access Token 请求一次性票据：

```http
POST /api/ws/ticket
Authorization: Bearer <access-token>
```

每一条 WebSocket 连接必须单独申请 Ticket。行情连接不能复用已被交易连接
消费的 Ticket：

```text
ws://gateway-host:8001/ws/market?ticket=<one-time-ticket>
```

Ticket 只允许消费一次。Access Token 到期或用户被禁用后连接会关闭。

## 客户端消息

增订合约：

```json
{"action":"subscribe","order_book_ids":["JD2609","RB2610"]}
```

退订合约：

```json
{"action":"unsubscribe","order_book_ids":["JD2609"]}
```

检测到单合约 `sequence_id` 缺口后重新读取最新快照：

```json
{"action":"resync","order_book_ids":["RB2610"]}
```

应用层心跳回复：

```json
{"action":"pong"}
```

服务端只接受合约表中已启用的 `order_book_id`，并由服务端解析真实交易所、
内部 symbol 和上游代码。单连接订阅上限由
`MARKET_CLIENT_SUBSCRIPTION_MAX_CODES_PER_CONNECTION` 控制。

## 服务端事件

- `SUBSCRIPTION_STATUS`：增订或退订结果、当前连接完整订阅集合和租约到期时间。
- `MARKET_SNAPSHOT`：订阅或重同步时从最新标准行情缓存生成的绝对快照。
- `MARKET_UPDATE`：同一标准 `MarketTick` 的实时更新。
- `MARKET_STATUS`：`CONNECTED`、`WAITING_MARKET_DATA` 或
  `MARKET_UNAVAILABLE`。
- `HEARTBEAT`：服务端心跳，客户端必须回复 `pong`。
- `AUTH_EXPIRED`：Access Token 到期。
- `ERROR`：经过清理的协议、合约、限额或服务错误。

`MARKET_SNAPSHOT` 和 `MARKET_UPDATE` 均保留 `order_book_id`、
`sequence_id`、`event_time`、`last_price`、累计成交量和买一/卖一字段。
金额及价格保持 Decimal 字符串。当前标准模型只有一档盘口，客户端不能
据此构造五档行情。

## 动态上游订阅

每条行情连接在 Redis 中登记短租约需求。行情 Gateway 存活期间自动续租，
退订或正常断开时立即删除；异常退出后由 TTL 清理。行情 Worker 的实际目标
集合为：

```text
活动订单 + 活动持仓 + 期权预订阅 + 客户端观察订阅
```

相同合约跨连接自动去重。最后一个需求消失并经过现有防抖后，Worker 才向
上游退订。配置项：

```text
MARKET_CLIENT_SUBSCRIPTION_TTL_SECONDS=90
MARKET_CLIENT_SUBSCRIPTION_MAX_CODES_PER_CONNECTION=50
```

## 快照与上游维修期

当前没有独立上游快照 API。Redis 已存在最新标准行情时，Gateway 立即发送
`MARKET_SNAPSHOT`；冷启动无缓存时先发送 `WAITING_MARKET_DATA`，上游第一条
Tick 以 `MARKET_UPDATE` 到达。上游快照接口完成后，应在服务端适配并写入
同一 `MarketTick`/最新行情缓存，客户端协议无需改变。

上游维修期间可注入测试 `MarketTick` 验证本地快照、增量、版本和重连；不得
在生产环境自动回退到模拟行情。真实上游增订、首 Tick 延迟和断线恢复仍需在
上游恢复后完成端到端验收。
