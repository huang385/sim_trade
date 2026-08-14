# 模块化单体目录边界

## 目标与迁移原则

项目仍是单进程体系、单代码仓库和单数据库的模块化单体。本阶段只调整 Python 物理边界和装配入口，不改变 API、表结构、订单状态机、资金公式、事件载荷或 Redis 键；股票业务没有实现。

迁移采用“新路径保存唯一新增实现，旧路径仅作兼容导出”的方式。已有的大型业务服务暂不复制：模块 `facade.py` 先作为唯一公共入口转发到原实现，调用方逐步迁到公共入口后，再逐文件移动实现。这样不会同时运行两套 Service，也不会因一次性移动 ORM 和全部业务代码产生难以审计的行为变化。

## 目录变化

重构前的业务代码主要按技术层全局堆放：

```text
app/
├── api/
├── enums/
├── matching/
├── models/
├── realtime/
├── repositories/
├── schemas/
├── services/
├── infrastructure/
├── workers/
├── core/
└── main.py
```

当前新增并启用的模块边界：

```text
app/
├── modules/
│   ├── auth/                 # facade.py、dependencies.py
│   ├── accounts/             # facade.py
│   ├── instruments/          # facade.py
│   ├── orders/               # facade.py、product_registry.py、matching.py
│   ├── trades/               # facade.py
│   ├── futures/              # facade.py
│   ├── options/              # facade.py
│   ├── risk/                 # facade.py、queries.py
│   ├── daily_settlement/     # facade.py
│   ├── market_data/          # facade.py、contracts.py
│   └── realtime/             # facade.py
├── shared/
│   └── enums/                # ProductFamily 唯一定义
├── infrastructure/
│   ├── database/
│   │   ├── model_registry.py
│   │   └── repository_adapters.py
│   └── market_data/
│       └── provider.py       # 旧路径兼容导出
├── api/                      # 保留路由，依赖模块公共入口
├── workers/                  # 保留进程入口，依赖模块公共入口/基础设施适配器
├── models/                   # ORM 渐进迁移期间的唯一模型实现
├── repositories/             # 渐进迁移期间的唯一 Repository 实现
├── schemas/                  # 保持 API 契约稳定
├── services/                 # 尚未物理迁移的大型唯一实现及少量兼容导出
├── matching/                 # 基础撮合类型；产品策略旧路径为兼容导出
├── realtime/                 # Gateway 运行实现，模块 facade 为公共入口
├── core/
└── main.py
```

## 模块职责与公共接口

| 模块 | 职责 | 主要公共接口 |
|---|---|---|
| `auth` | 登录、Token、密码、限流和用户管理装配 | `AuthService`、`AdminUserService`、`get_auth_service` |
| `accounts` | 账户、归属、授权和访问范围 | `AccountService`、`AccountAuthorizationService`、`AccountAccessScope` |
| `instruments` | 合约、费率、保证金规则查询 | `InstrumentService`、`FeeRuleService`、`MarginRuleService`、`RuleQueryService` |
| `orders` | 下单、撤单、订单事件、产品及撮合策略注册 | `OrderService`、`OrderCancellationService`、`ProductStrategyRegistry`、`MatchingStrategyRegistry` |
| `trades` | 成交公共编排 | `TradeSettlementService`、`TradeSettlementResult` |
| `futures` | 期货手续费、保证金及已实现盈亏计算入口 | `FeeCalculator`、`MarginCalculator`、`MarginReleaseCalculator`、`RealizedPnlCalculator` |
| `options` | 期权权利金、保证金、权限和成交后调整 | `OptionPremiumCalculator`、`OptionMarginCalculatorResolver`、`OptionTradeSettlementStrategy` |
| `risk` | 风险查询、监控与强平编排 | `RiskQueryService`、`RiskMonitorService`、`LiquidationService` |
| `daily_settlement` | 日终批次及不可变事实回放 | `DailySettlementService`、`SettlementReplayService` |
| `market_data` | 行情订阅、标准化、校验、快照和 Provider 端口 | `MarketDataProvider`、`MarketDataSubscription`、`MarketDataService` |
| `realtime` | Ticket、快照、事件投影、实时 PnL 应用入口 | `WebSocketTicketService`、`SnapshotService`、`RealtimeEventProjectionService` |

模块外部统一从 `app.modules.<name>` 导入。`orders` 和 `market_data` 使用惰性导出，避免应用装配阶段因 Service 反向加载公共包而形成导入环。

## 依赖方向

```text
API / Worker
    -> app.modules.<module> 公共入口
    -> infrastructure 适配器

业务模块
    -> shared 稳定类型
    -> Protocol / 公共 Facade

infrastructure
    -> 实现端口、数据库和消息设施
```

当前架构测试禁止：`shared -> app.modules`、API/Worker 直接导入 `app.repositories`、基础设施导入期货/期权/订单模块内部路径、期货和期权模块相互导入，以及模块依赖图出现环。

## 唯一实现与兼容路径

以下实现已经物理迁移，旧路径只保留无副作用的导出：

- 产品族枚举：`app.shared.enums.product`；`app.enums.product_enums` 为兼容路径。
- 产品策略注册表：`app.modules.orders.product_registry`；`app.services.product_strategy_registry` 为兼容路径。
- 产品撮合策略：`app.modules.orders.matching`；`app.matching.product_strategy` 为兼容路径。
- 行情 Provider Protocol：`app.modules.market_data.contracts`；`app.infrastructure.market_data.provider` 为兼容路径。

`app.models`、`app.repositories`、多数 `app.services`、`app.realtime` 仍保留，是因为它们被迁移脚本、测试 Fixture、SQLAlchemy relationship 字符串、Worker 启动入口及大量稳定导入共同使用。当前 Facade 只转发这些唯一实现，没有复制类、Schema 或业务算法。后续应按 instruments、auth、accounts、orders、trades、futures、options、risk、daily_settlement、market_data、realtime 的顺序逐个移动，并在最后一个旧调用方消失后删除对应兼容文件。

## ORM 与 Alembic

`app.infrastructure.database.model_registry` 是唯一模型注册入口：它先加载 `app.models` 中全部 ORM 类，再公开同一个 `Base.metadata`。`app.main` 和 `alembic/env.py` 均使用该入口。架构测试校验所有 `app.models.__all__` 模型都在 metadata 中，并校验没有重复 Table key。目录调整本身不创建 Alembic revision，也不修改历史迁移。

## 性能约束

Facade 不查询数据库、不重复授权、不重新加载 Instrument 或 Order。它只是稳定导入边界，因此典型请求 SQL 次数不增加。现有登录授权、日终账户批量加载和 WebSocket 固定查询数测试继续作为回归基线。

## 股票模块接入点

未来股票代码放在 `app/modules/stocks`，通过 orders 的产品策略注册接口、trades 的成交编排接口、instruments 的证券标识接口、market_data 的 Provider Protocol、accounts 的资金事实接口和 realtime 的事件/快照契约接入。股票模块不得导入 futures/options 内部实现；T+1、股票手续费、可卖数量和股票撮合差异均留在 stocks 自身策略中。
