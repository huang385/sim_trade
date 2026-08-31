# 模拟交易系统外部接口接入说明

> 本文面向交易客户端/合作方开发人员，说明当前已开放的账户私有 REST API 与实时 WebSocket 推送协议。所有接口仅允许访问当前登录用户被授权的账户；管理员可访问全部账户。

## 1. 接入概览

| 能力 | 通道 | 用途 |
| --- | --- | --- |
| 登录、下单、撤单、查询 | HTTPS REST | 发起指令及补拉权威数据 |
| 订单、成交、持仓、资金、风控变化 | WebSocket | 接收自己账户的实时状态变更 |

WebSocket 是系统提供的实时“回调”机制，当前**不提供由接入方配置 HTTP 回调地址的 Webhook**。客户端应以 REST 查询/订阅后的 `SNAPSHOT` 为初始状态，再消费 WebSocket 增量事件。

示例以下述地址为准，请由部署方替换为实际域名和端口：

```text
REST_BASE_URL = https://<api-host>
WS_URL        = wss://<ws-host>/ws/trading
```

所有金额、价格、保证金、盈亏等 `Decimal` 字段在 JSON 中可能被编码为数值或十进制字符串。接入方应按十进制处理，禁止使用二进制浮点数累计计算。

## 2. 认证与账户权限

### 2.1 登录

`POST /api/auth/login`

```json
{
  "username": "trader_a",
  "password": "your-password"
}
```

成功后返回短期 `access_token`；刷新令牌由服务端写入仅 HTTP Cookie，不会出现在响应 JSON 中。

```json
{
  "access_token": "eyJ...",
  "token_type": "bearer",
  "expires_in": 1800,
  "user": {
    "user_id": "U001",
    "username": "trader_a",
    "display_name": "交易员 A",
    "role": "USER"
  }
}
```

后续 REST 请求均携带：

```http
Authorization: Bearer <access_token>
Content-Type: application/json
```

Token 即将或已经失效时，调用 `POST /api/auth/refresh`（浏览器/客户端需保留服务端下发的 Cookie）获取新的 access token。退出登录使用 `POST /api/auth/logout`，返回 `204 No Content`。

### 2.2 获取本人及可访问账户

`GET /api/auth/me`

该接口返回当前用户及其有权限的账户完整摘要。也可使用：

- `GET /api/accounts`：列出可访问账户；
- `GET /api/accounts/{account_id}`：查询一个账户的资金与静态账户信息。

账户响应重点字段：

| 字段 | 含义 |
| --- | --- |
| `cash_balance` | 现金余额 |
| `available_cash` | 可用资金 |
| `frozen_cash` / `frozen_margin` / `frozen_commission` | 委托冻结的现金、保证金、手续费 |
| `equity` | 账户权益 |
| `used_margin` | 已占用保证金 |
| `realized_pnl` / `unrealized_pnl` | 已实现/未实现盈亏 |
| `daily_pnl` / `cumulative_net_pnl` | 当日/累计净盈亏 |
| `risk_ratio` / `risk_state` / `risk_available_cash` | 风险率、风险状态和风控可用资金 |
| `status` / `trading_day` | 账户交易状态及交易日 |

## 3. 下单

### 3.1 衍生品订单（期货、期权等）

`POST /api/orders`

```json
{
  "client_order_id": "cli-20260828-00001",
  "account_id": "A001",
  "exchange_id": "SHFE",
  "symbol": "RB2610",
  "direction": "BUY",
  "offset_flag": "OPEN",
  "order_type": "LIMIT",
  "limit_price": "3200.00",
  "volume": 2
}
```

请求字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `client_order_id` | 是 | 接入方订单号，1–64 字符；同一账户内是幂等键 |
| `account_id` | 是 | 已授权的账户编号 |
| `exchange_id` | 是 | 交易所代码，如 `SHFE` |
| `symbol` | 是 | 合约代码，如 `RB2610` |
| `direction` | 是 | `BUY` 或 `SELL` |
| `offset_flag` | 是 | `OPEN`、`CLOSE`、`CLOSE_TODAY`、`CLOSE_YESTERDAY` |
| `order_type` | 否 | 默认 `LIMIT`；可取 `LIMIT`、`COUNTERPARTY`、`LAST`、`MARKET`，实际可用性由产品和行情条件决定 |
| `limit_price` | 条件必填 | `LIMIT` 必填且大于 0；非限价单不得传该字段 |
| `volume` | 是 | 正整数，仍须符合合约最小变动、数量和交易规则 |

### 3.2 下单前：查询可交易标的与交易参数

接入方不应在程序中写死合约的价格档位或下单数量上限。下单前应通过以下只读接口加载服务端当前允许交易的标的和参数：

| 接口 | 适用标的 | 关键返回字段 |
| --- | --- | --- |
| `GET /api/instruments` | 当前可交易期货列表 | `exchange_id`、`symbol`、`instrument_type`、`price_tick`、`min_volume`、`max_volume`、`is_active`、`is_tradeable` |
| `GET /api/instruments/search?q=...` | 搜索可交易衍生品 | 合约目录及价格档位 |
| `GET /api/instruments/stocks/search?q=...` | 搜索股票、可转债 | 证券目录及价格档位 |

实际下单仍以服务端校验为准：标的必须存在且可交易；委托数量必须落在该标的的 `min_volume` 到 `max_volume` 范围；委托/解析后的价格必须是 `price_tick` 的整数倍。

### 3.3 买卖方向与开平标志（衍生品）

`direction` 说明订单的买卖方向，`offset_flag` 说明是开仓还是平仓；两者必须组合理解。

| 交易目的 | `direction` | `offset_flag` | 结果 |
| --- | --- | --- | --- |
| 建多仓 | `BUY` | `OPEN` | 新增长方向持仓 |
| 建空仓 | `SELL` | `OPEN` | 新增空方向持仓 |
| 平多仓 | `SELL` | `CLOSE` / `CLOSE_TODAY` / `CLOSE_YESTERDAY` | 减少多方向持仓 |
| 平空仓 | `BUY` | `CLOSE` / `CLOSE_TODAY` / `CLOSE_YESTERDAY` | 减少空方向持仓 |

`offset_flag` 取值规则：

| 值 | 含义 | 使用场景 |
| --- | --- | --- |
| `OPEN` | 开仓 | 建立新的多仓或空仓 |
| `CLOSE` | 普通平仓 | 由系统按交易规则处理可平持仓 |
| `CLOSE_TODAY` | 平今仓 | 仅平当前交易日开立的持仓 |
| `CLOSE_YESTERDAY` | 平昨仓 | 仅平当前交易日前开立的持仓 |

平仓前应先查询 `GET /api/positions?account_id=...`，以 `direction`、`today_volume`、`yesterday_volume`、`frozen_volume` 和 `available_volume` 判断可平数量。可用数量不足、开平类型不符合交易规则时，服务端会拒绝请求；不能将平仓委托当作开仓委托使用。

### 3.4 订单类型与定价规则（衍生品）

订单类型只适用于 `POST /api/orders`，由 `order_type` 指定。除 `LIMIT` 外，系统都在订单受理时从**当前有效行情快照**解析一次成交/委托价格，返回的 `resolved_price`、行情快照字段及实际成交结果才是最终依据。

| `order_type` | `limit_price` | 受理时的价格规则 | 行情依赖与注意事项 |
| --- | --- | --- | --- |
| `LIMIT` | 必填，> 0 | 使用客户端提交的限价 | 不读取行情定价；价格必须匹配 `price_tick` |
| `COUNTERPARTY` | 不传 | `BUY` 取卖一 `ask1`；`SELL` 取买一 `bid1` | 要求对应一档价格和一档可用量均存在 |
| `LAST` | 不传 | 取最新价 `last_price` | 要求最新价存在、有效且交易日匹配 |
| `MARKET` | 不传 | `BUY` 以卖一、`SELL` 以买一为基础，按服务端配置的最大滑点生成保护价，并按价格档位向不利方向取整 | 要求对手一档及可用量存在；返回中的 `market_protection_price` 是该市价单的保护边界 |

对于 `COUNTERPARTY`、`LAST`、`MARKET`，如果行情尚未准备完成，可能返回 `503`，例如 `ORDER_MARKET_DATA_PREPARING` 或 `ORDER_PRICE_MARKET_DATA_UNAVAILABLE`；客户端应短暂退避后使用**相同的 `client_order_id`**重试。行情交易日不一致、盘口倒挂、盘口字段缺失或对应档位无量也会被拒绝。

市价单在一次撮合后如仍有剩余数量，系统可能自动撤销剩余部分，因此接入方必须根据最终订单状态区分 `FILLED`、`CANCELLED` 与 `PARTIALLY_CANCELLED`，不能假设市价单必然全部成交。

示例：对手价买入（不传 `limit_price`）：

```json
{
  "client_order_id": "cli-20260828-00002",
  "account_id": "A001",
  "exchange_id": "SHFE",
  "symbol": "RB2610",
  "direction": "BUY",
  "offset_flag": "OPEN",
  "order_type": "COUNTERPARTY",
  "volume": 1
}
```

### 3.5 股票订单

`POST /api/stock/orders`

请求字段与上述相同，但**不传** `offset_flag`，且仅接收 `LIMIT` 单。该接口仅适用于 `STOCK` / `SECURITIES_CASH` 账户，并需要服务端启用股票委托功能。

股票 `direction` 的含义为：`BUY` 买入证券，系统冻结委托金额及预估手续费；`SELL` 卖出已有多头证券，系统冻结可卖持仓数量。股票接口不支持做空、开平标志、市价单、对手价单或最新价单。卖出前应确认该证券持仓的 `available_volume` 足够。

### 3.6 可转债订单

`POST /api/convertible-bond/orders`

与股票订单相同：不传 `offset_flag`，仅限价单，适用于现金证券账户。

### 3.7 下单结果、幂等与状态

成功响应为订单完整快照（`200 OK`），核心字段如下：

```json
{
  "order_id": "O20260828ABCDEF1234567890",
  "client_order_id": "cli-20260828-00001",
  "account_id": "A001",
  "order_book_id": "SHFE.RB2610",
  "exchange_id": "SHFE",
  "symbol": "RB2610",
  "instrument_type": "FUTURES",
  "direction": "BUY",
  "offset_flag": "OPEN",
  "order_type": "LIMIT",
  "limit_price": "3200.00",
  "total_volume": 2,
  "traded_volume": 0,
  "remaining_volume": 2,
  "cancelled_volume": 0,
  "average_price": null,
  "frozen_margin": "0.00",
  "frozen_cash": "0.00",
  "frozen_commission": "0.00",
  "status": "ACCEPTED",
  "submit_status": "ACCEPTED",
  "created_at": "2026-08-28T01:20:00+00:00",
  "updated_at": "2026-08-28T01:20:00+00:00"
}
```

订单状态：`ACCEPTED`、`PARTIALLY_FILLED`、`FILLED`、`CANCELLED`、`PARTIALLY_CANCELLED`、`REJECTED`。数量始终满足：

```text
total_volume = traded_volume + remaining_volume + cancelled_volume
```

请保存系统生成的 `order_id`，后续撤单、精确查询和成交关联均使用它。

同一 `account_id + client_order_id` 重复提交且请求内容一致时，系统返回原订单，不重复冻结资金；同一幂等键但内容不同会返回 `409` 冲突。网络超时后请使用**同一个** `client_order_id` 重试，不要生成新编号。

## 4. 撤单

### 4.1 衍生品撤单

`POST /api/orders/{order_id}/cancel`

### 4.2 股票、可转债撤单

- `POST /api/stock/orders/{order_id}/cancel`
- `POST /api/convertible-bond/orders/{order_id}/cancel`

请求体一致：

```json
{
  "account_id": "A001"
}
```

撤单会撤销全部剩余未成交数量，并返回更新后的订单快照。已撤订单重复撤单为幂等返回；已全部成交、已拒绝或其他不可撤状态将返回 `ORDER_NOT_CANCELLABLE`。部分成交后撤单的最终状态为 `PARTIALLY_CANCELLED`。

## 5. 订单、成交、持仓与资金查询

这些接口均要求 Bearer Token，且服务端校验账户归属。普通用户不能跨账户查询。

| 目的 | 接口 | 说明 |
| --- | --- | --- |
| 单笔订单 | `GET /api/orders/{order_id}` | 根据系统订单号查询 |
| 订单列表 | `GET /api/orders?account_id=A001&after_id=0&limit=100` | `limit` 1–500，旧式列表接口 |
| 订单游标分页 | `GET /api/orders/page?account_id=A001&trading_day=2026-08-28&cursor=...&limit=100` | 推荐；返回 `items`、`next_cursor`、`has_more` |
| 股票订单手续费快照 | `GET /api/stock/orders/{order_id}/fee-components` | 返回订单受理时锁定的手续费组件 |
| 单笔成交 | `GET /api/trades/{trade_id}` | 根据系统成交号查询 |
| 成交列表 | `GET /api/trades?account_id=A001&after_id=0&limit=100` | 也可传 `order_id`；普通用户至少要指定其一 |
| 成交游标分页 | `GET /api/trades/page?account_id=A001&cursor=...&limit=100` | 推荐分页方式 |
| 平仓成交分配明细 | `GET /api/trades/{trade_id}/position-allocations` | 返回平仓对应的逐笔持仓、保证金、手续费和盈亏 |
| 当前持仓 | `GET /api/positions?account_id=A001` | 返回按合约和多空汇总后的持仓 |
| 实时账户盈亏/资金 | `GET /api/accounts/A001/pnl/realtime` | 返回实时或持久化兜底数据，并用 `data_source` 标识来源 |
| 账户交易快照 | `GET /api/accounts/A001/trading-snapshot` | 一次返回账户、实时盈亏及持仓，适合断线恢复 |
| 单持仓实时盈亏 | `GET /api/positions/{position_id}/pnl/realtime` | 查询指定持仓 |

成交记录主要字段包括 `trade_id`、`order_id`、`account_id`、合约信息、`trade_price`、`trade_volume`、`turnover`、`margin`、`commission`、`realized_pnl`、`trade_time`。持仓主要字段包括 `position_id`、`direction`（`LONG`/`SHORT`）、总/今/昨/冻结/可用数量、均价、成本、保证金、已实现和浮动盈亏等。

## 6. 实时订单、成交、持仓和资金推送

### 6.1 建立连接

1. 通过登录或刷新取得 access token。
2. 携带 Bearer Token 调用 `POST /api/ws/ticket`。
3. 使用返回的单次 ticket 建立 WebSocket 连接。
4. 发送订阅消息。

获取 ticket：

```http
POST /api/ws/ticket
Authorization: Bearer <access_token>
```

```json
{
  "ticket": "one-time-ticket",
  "expires_in": 30
}
```

Ticket 为一次性短期凭证（当前约 30 秒），不可复用；不要将 access token 放到 WebSocket URL。连接示例：

```text
wss://<ws-host>/ws/trading?ticket=<one-time-ticket>
```

连接成功后发送：

```json
{
  "action": "subscribe",
  "account_ids": ["A001"]
}
```

取消订阅：

```json
{
  "action": "unsubscribe",
  "account_ids": ["A001"]
}
```

订阅只会成功覆盖当前用户有权限的账户。首次成功订阅的第一条业务消息必为 `SNAPSHOT`，包含账户、实时盈亏、当前活动订单、当日成交和持仓；客户端应先以它覆盖本地状态，再消费后续增量。

### 6.2 统一事件信封

所有服务器消息具有以下结构：

```json
{
  "event_id": "EVT-...",
  "event_type": "ORDER_UPDATED",
  "account_id": "A001",
  "entity_id": "O20260828...",
  "account_type": "FUTURES",
  "instrument_type": "FUTURES",
  "occurred_at": "2026-08-28T01:20:05+00:00",
  "version": "1756344005000-0",
  "business_version": "12345",
  "realtime_version": null,
  "payload": {}
}
```

- `version` 是消息流游标，仅用于传输顺序和快照屏障；不应用于判断业务状态新旧。
- `business_version` 用于同一订单、成交、持仓或账户事实的版本判断。
- `realtime_version` 用于实时估值事件。
- `payload` 是该事件对应实体的**绝对快照**，不是增量差值。客户端应覆盖对应实体，不能对金额或数量自行累加。

### 6.3 业务事件

| `event_type` | 含义与处理建议 |
| --- | --- |
| `ORDER_CREATED` | 订单已受理；写入/覆盖该订单 |
| `ORDER_UPDATED` | 订单成交数量、均价、保证金等变更；覆盖该订单 |
| `ORDER_CANCELLED` | 订单已撤或部分撤；覆盖该订单 |
| `TRADE_CREATED` | 新成交；按 `trade_id` 去重后写入成交列表 |
| `POSITION_UPDATED` | 持仓数量、成本、保证金或已实现盈亏变更；覆盖该持仓 |
| `POSITION_CLOSED` | 持仓已清零；从活动持仓列表移除 |
| `ACCOUNT_FACT_UPDATED` | 账户已落库的现金、冻结、保证金、手续费、已实现盈亏等事实更新；覆盖相应字段 |
| `ACCOUNT_PNL_UPDATED` / `PNL_UPDATED` | 盘中浮盈、动态权益、可用资金、期权市值等实时估值更新；覆盖相应实时字段 |
| `RISK_STATE_CHANGED` / `RISK_WARNING` | 风险状态或预警变更；刷新风险展示和交易权限判断 |
| `LIQUIDATION_*` | 强平生命周期事件；展示并限制普通下单 |

订单类 `payload` 至少包含订单号、客户订单号、账户、合约、方向、开平、订单类型、数量字段、订单状态、冻结资源和更新时间。成交类包含 `trade_id`、`order_id`、成交价、成交量、手续费、保证金、已实现盈亏和成交时间。资金和持仓事件包含对应实体最新的绝对金额/数量。

### 6.4 控制事件与断线恢复

- 收到 `HEARTBEAT` 后尽快回复 `{"action":"pong"}`。
- 收到 `ERROR` 时读取 `payload.error_code` 和 `payload.message`，修正订阅请求或重新认证。
- 收到 `AUTH_EXPIRED`、`RESYNC_REQUIRED`，或连接以 `4401`、`4403`、`4408`、`4450`、`4451`、`4452`、`4503` 等状态码关闭时，应重新登录/刷新 token、申请新 ticket、重连并重新订阅。
- 任意重连后不要按旧游标续接；以新 `SNAPSHOT` 重建本地状态，再接收新的增量事件。

## 7. 错误约定

业务异常统一返回：

```json
{
  "success": false,
  "error_code": "INSUFFICIENT_AVAILABLE_CASH",
  "message": "账户可用资金不足"
}
```

常见 HTTP 状态码：

| 状态码 | 含义 |
| --- | --- |
| `400` | 请求或业务校验失败 |
| `401` | 未登录、Token 无效或用户不可用 |
| `403` | 无账户/资源访问权限 |
| `404` | 资源不存在或不可见 |
| `409` | 幂等键内容冲突、订单不可撤等状态冲突 |
| `422` | 违反交易业务规则，如账户不可交易、合约不可交易、资金/持仓不足 |
| `429` | 频率受限 |
| `503` | 行情、订阅或其他安全依赖暂不可用，可按退避策略重试 |

下单或撤单出现网络超时、`5xx` 时，先查询订单或以原 `client_order_id` 重试下单；禁止盲目以新订单号重复提交。

## 8. 推荐客户端流程

```text
登录 → GET /api/auth/me 确认账户 → 申请 WS ticket → 订阅账户
     → 接收 SNAPSHOT 建立本地状态 → REST 下单/撤单
     → 消费订单、成交、持仓、资金增量事件
     → 断线后重新认证、重新订阅、以新 SNAPSHOT 恢复
```

REST 查询是审计和补数的权威入口；WebSocket 用于降低延迟和驱动界面实时更新。两者配合使用，且始终以系统返回的 `order_id`、`trade_id`、`position_id` 作为实体主键。
