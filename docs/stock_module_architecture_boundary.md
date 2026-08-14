# 股票模块接入前的产品架构边界

## 本阶段范围

本阶段只建立产品分发和分层边界，不实现股票下单、资金、持仓、T+1、费用、撮合、估值、推送或日终逻辑。现有 API 路径、数据库表、Worker 入口、Outbox、Redis Stream 和 WebSocket 协议保持兼容；没有新增 Alembic 迁移，也没有批量移动目录。

## 当前模块结构

现有目录继续承担原职责，仅增加最小扩展点：

```text
app/
├── api/                         协议、认证、授权、Schema 与响应转换
├── enums/                       核心枚举唯一事实来源
│   └── product_enums.py         产品族（期货、期权）
├── schemas/
│   └── order_schema.py          公共请求字段与衍生品请求字段分层
├── repositories/                查询、行锁和 ORM 对象读写
├── services/
│   └── product_strategy_registry.py
├── matching/
│   └── product_strategy.py      撮合策略的产品分发入口
├── infrastructure/market_data/
│   └── provider.py              行情提供方的窄协议边界
├── realtime/                    统一事件、快照和 WebSocket Gateway
└── workers/                     消费、调用服务、ACK/重试/死信编排
```

未创建空的 `stock` 目录或空 Repository。等股票规则真正实现时，再按实际职责增加模块，避免只有命名没有业务语义的占位代码。

## 产品注册中心

`ProductStrategyRegistry` 只接受服务端已加载或已持久化的 `Instrument.instrument_type`、`Order.instrument_type`、`Position.instrument_type`。客户端订单 Schema 不含产品类型，因此客户端额外提交的 `instrument_type` 不能改变策略选择。

当前映射如下：

| InstrumentType | ProductFamily | 当前策略 |
| --- | --- | --- |
| `FUTURES` | `FUTURES` | `FuturesProductStrategy` |
| `FUTURES_OPTION` | `OPTIONS` | `OptionProductStrategy` |
| `INDEX_OPTION` | `OPTIONS` | `OptionProductStrategy` |

`STOCK` 和其他未注册值统一抛出 `PRODUCT_STRATEGY_NOT_REGISTERED`。本阶段没有注册返回虚假结果的股票策略。

注册中心是产品身份分发入口，不是包含全部公式的万能接口。现有期货和期权的保证金、手续费、权利金、持仓及盈亏计算仍由原有专用服务完成。

## 关键调用链

### 下单

```text
Order API
→ 认证与账户授权
→ RuleQueryService 加载服务端 Instrument 与规则
→ ProductStrategyRegistry
→ 原有产品校验和资金/保证金冻结
→ 创建 Order 与 Outbox
→ OrderService 统一提交事务
```

公共 Schema 包含账户、合约、方向、价格类型、价格和数量；`DerivativeOrderCreateRequest` 才要求 `offset_flag`。现有 `OrderCreateRequest` 保留为衍生品兼容名称，API 行为不变。未来股票请求不能通过伪造 `OPEN/CLOSE` 接入。

### 撤单

```text
加载并锁定 Order
→ 账户授权与幂等检查
→ 按持久化 Order.instrument_type 解析产品
→ 原有期货/期权资源释放
→ 更新 Order 与 Outbox
→ Service 提交事务
```

### 撮合与成交结算

```text
MarketTickMatchingService（协调器）
→ PostgreSQL Order 或不可变订单候选快照
→ ProductStrategyRegistry
→ MatchingStrategyRegistry
→ DerivativeMatchingStrategy
→ 原有 MatchingEngine
→ TradeSettlementService
→ 校验 Order 与 Instrument 产品类型一致
→ 原有期货或期权结算逻辑
→ Trade/Position/Account/Outbox
```

`DerivativeMatchingStrategy` 只是包裹原撮合引擎，未改变 Tick 顺序、成交价格、盘口数量、活动订单索引、Pending 恢复或至少一次投递语义。未来股票撮合必须单独注册，不能把交易时段、涨跌停和集合竞价塞入衍生品策略。

### 实时估值与日终

活动持仓缓存、实时 PnL 计算、PnL 持久化和强平候选均先解析持仓的服务端产品类型。日终预检和成交回放也先解析合约产品类型；未知产品不能进入衍生品逐日结算。

期货和期权现有估值及日终公式没有调整，只改变了进入这些公式前的显式分发方式。

## 分层检查结果

### API 与 Schema

API 中没有新增保证金、手续费、持仓或 T+1 计算。请求仍走统一 `/api/orders` 链路。公共订单字段和衍生品 `offset_flag` 已分开，现有请求及响应保持兼容。

### Enum

以下核心枚举各只有一个类定义：

- `AccountType`：`app/enums/account_enums.py`
- `InstrumentType`：`app/enums/option_enums.py`
- `MarketType`、`ExchangeID`：`app/enums/market_enums.py`
- `OrderType`：`app/enums/order_enums.py`

`app/core/enums.py` 仅保留兼容导入，不再重复定义同名枚举。

### Models 的股票兼容问题清单

本阶段没有修改数据库。后续股票模块至少需要解决：

1. `Order.offset_flag` 非空并带有衍生品开平语义；股票应有独立买卖业务事实，不能伪填 `OPEN/CLOSE`。
2. `Trade.offset_flag` 和部分费用审计字段以衍生品成交为中心；股票费用应表达佣金、印花税、过户费等独立事实。
3. `Position.direction` 使用 `LONG/SHORT`，数量守恒只考虑冻结量；股票需要可卖数量、当日买入不可卖数量以及公司行为等事实。
4. `Account` 的可用资金和风险字段以保证金账户为中心；股票需要现金占用和在途交收口径，不能伪造保证金。
5. `Instrument` 假设合约乘数及衍生品属性；股票最小申报单位、价格笼子、涨跌停和交易板块规则需要明确字段或扩展表。
6. 当前日终事实属于衍生品逐日结算；股票应有独立清算/交收和 T+1 结转事实。

这些问题应通过明确字段或扩展表解决，而不是用衍生品字段保存虚假值。

### Repository

Repository 保持数据访问职责。自动化架构测试检查 Repository 不调用 `commit()`/`rollback()`，也不依赖 Service、Infrastructure、Matching、Realtime 或 Worker 上层模块。产品选择和资金公式仍在 Service/策略层。

### Infrastructure 与 Worker

`MarketDataProvider` 和 `MarketDataSubscription` 约束连接、订阅、回调和关闭能力，当前 `RemoteFeedClient` 结构化兼容该接口。行情适配器不处理订单合法性、资金、T+1 或持仓修改。

Worker 入口未移动；Worker 继续负责消费、调用 Service，以及 ACK、重试和死信编排。

### Realtime

继续使用统一 WebSocket Gateway。事件 Envelope 向后兼容地支持可选 `account_type`、`instrument_type` 和既有 `business_version`；事件投影从服务端事实载荷提取这些维度。完整快照原本已通过 Account、Order、Position Schema 携带账户和合约类型，估值 Hash 继续携带业务版本。

## SQL 与迁移影响

本阶段没有新增模型字段或 Alembic 迁移。产品分发复用当前请求内已加载的 Instrument/Order/Position，不为策略选择新增查询，也没有在循环内按产品回查数据库。因此典型下单、撤单、撮合和估值链路的 SQL 查询次数不因注册中心而增加。

## 下一阶段股票接入点

建议按以下顺序推进，并在每一步保留“未注册即失败”：

1. 明确股票 Instrument、账户、订单、成交、持仓和交收事实模型，并生成迁移。
2. 增加股票专用请求 Schema，不要求 `offset_flag`。
3. 实现并注册股票校验、现金冻结、费用、撤单释放和成交结算策略。
4. 实现股票可卖数量与 T+1 结转，不复用衍生品平今/平昨分配。
5. 实现独立 `StockMatchingStrategy`，处理时段、申报单位、涨跌停和集合竞价。
6. 接入股票行情 Adapter，并继续输出标准 Tick。
7. 实现股票估值、风险和清算/交收策略。
8. 在统一 Outbox、Redis Stream 和 WebSocket 协议中增加股票事实事件。
9. 补齐股票单元、集成、性能、授权和幂等测试后再开放 API。
