# ETF 二级市场模块

第一阶段只实现沪深 ETF 的二级市场现金买卖，不包含申购赎回、ETF 期权和融资融券。

## 复用边界

- ETF 使用独立领域类型 `instrument_type=ETF`、`market_type=FUND`。
- 行情复用 `securities-market-data`，撮合复用 `securities-matching`，估值与日终复用现金证券链路，不新增常驻进程。
- 下单和撤单使用独立 `/api/etf/orders` 接口；合约搜索使用 `/api/instruments/etfs/search`。
- ETF 数量单位是“份”。买入按合约 `round_lot` 的整数倍；卖出零份只能与整手合并，或将当前可用零份一次卖完。
- 是否允许当日回转按每只合约的 `market_tplus` 执行：`0` 为 T+0，`1` 为 T+1。

## 参考数据

`reference-sync` 从 YMM Data 的 ETF 目录同步 `fund_type`、`market_tplus`、`round_lot`、`least_redeem`、参考标的和价格步长，并为每只 ETF 生成交易规则及每日交易事实。

ETF 不套用股票税费。默认只生成券商净佣金和适用的 ETF 经手费；`BondIndex`、`Money` 类型免生成经手费，不生成印花税、股票过户费或证管费。券商佣金由 `ETF_DEFAULT_COMMISSION_RATE` 和 `ETF_DEFAULT_MINIMUM_COMMISSION` 配置。

## 发布顺序

1. 部署并执行 Alembic `20260903_0042`。
2. 部署新版 `reference-sync`，执行合约、现金证券规则/日事实、交易时段和费用同步。
3. 核对 ETF 合约数量、T+0/T+1 分类、日事实及费用组件。
4. 先保持 `ETF_ORDER_ENTRY_ENABLED=false`、`ETF_MATCHING_ENABLED=false`。
5. 完成 YMM Live 订阅灰度后，依次开启下单和撮合开关。

股票和可转债开关保持原语义；仅开启 `STOCK_MATCHING_ENABLED` 不会撮合 ETF。
