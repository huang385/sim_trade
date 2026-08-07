# 手工日终结算

本阶段只有一次性管理员命令，不包含常驻 Worker、调度器或管理 API：

```bash
python -m app.scripts.run_daily_settlement --trading-day 2026-08-06
```

命令严格接受 `YYYY-MM-DD`，成功返回 0，业务失败返回非零并输出批次、阶段、
账户（如适用）、错误码、可重试标记和重试命令。批次状态为 `RUNNING`、
`FAILED`、`COMPLETED`；同一交易日在数据库中唯一。失败重跑复用原批次和
冻结价。账户、持仓、到期及 Outbox 事实采用整批单事务提交，不存在部分账户
已过账的中间状态；已完成批次只重试缓存恢复，不再次过账。

## 互斥和事务

普通下单、成交结算、外部撤单、期权保证金调整和 PnL 落库事务先取得同一
PostgreSQL advisory transaction shared lock。日结进程在独立连接上持有对应
session exclusive lock，直到整批退出。这样排他锁成功本身就是在途写事务已
排空的屏障；批次建立后交易 Service 还会按数据库状态拒绝旧交易日写入。
日结内部撤单仍复用现有撤单 Service、冻结释放和 Outbox 事务。

整批先冻结全部价格，再在一个数据库事务内锁定账户、持仓和明细，回放全部
不可变成交及平仓分配。所有账户资金、持仓、持仓明细、账户/持仓日结事实、
到期事实及 Outbox 一次提交；任一守恒校验失败时全部回滚。Repository 不提交
或回滚。

## 结算价

结算统一使用现有 `MarketTick.last_price`，原因是当前 Tick 模型没有独立的
交易所结算价字段，而 `last_price` 是真实行情链路稳定提供且具有事件时间、
交易日和事件编号审计信息的唯一成交价格。实现不使用 `pre_close`、开高低价
或静默回退链。

每个唯一 `(exchange_id, symbol)` 只调用一次
`tick_store.get_latest(exchange_id, symbol)`。必须同时满足 Tick 存在、标识
完整匹配、`last_price` 是有限正 Decimal、交易日一致、时间不过旧且不来自
未来。期货、期权及期权标的全部验证后，在任何账户过账前一次性写入
`instrument_settlement_price`。失败续跑只读取数据库冻结价。

## 会计处理

日终不读取 Redis 实时 PnL，也不使用数据库当前 `daily_pnl` 反推结果。权威输入
只有历史结算事实、全部 Trade、TradePositionAllocation、PositionDetail 的
不可变开仓字段、冻结结算价及当日规则。即使一笔持仓今日已全部平仓，也会
进入 `daily_position_settlement`。

期货逐日盯市：

```text
(结算价 - PositionDetail.pnl_base_price)
× multiplier_snapshot × remaining_volume × 方向符号
```

结果进入现金，剩余明细的 `pnl_base_price` 更新为本次结算价；原始
`open_price`、开仓日、Trade 和原始保证金审计字段不变。盘中累计浮盈仍按
原始开仓价展示，账户资金估值则只使用新结算基准后的价差，避免已入现金的
前一日盯市盈亏被重复计入权益。

全部期权到期均现金差额结算，不生成期货持仓：

```text
CALL = max(标的结算价 - 执行价, 0)
PUT  = max(执行价 - 标的结算价, 0)
现金额 = 内在价值 × multiplier_snapshot × remaining_volume
```

多头收款、空头付款，持仓及明细数量归零并释放保证金。权利金不再次收付。
未到期期权仅以“期权结算价 × 乘数 × 数量”更新市值；空头保证金复用现有
期权计算器和持仓内不可变规则快照，不把未实现市值转入现金。

剩余持仓统一转为昨仓，`today_volume=0`，明细不删除，开仓日期继续作为后续
`CLOSE_TODAY` / `CLOSE_YESTERDAY` 分配依据。Order、Trade、PositionDetail
和 Outbox 均不清空，也不创建重复的订单或成交历史表。

## PostgreSQL、Outbox 和 Redis

PostgreSQL 是唯一资金事实。账户事实明确区分期货现金结算、期权经济盈亏、
期权权利金现金流、到期现金流、平仓盈亏和手续费；持仓事实保存期初昨仓、
今日开仓、平今、平昨、期末数量、前后基准及前后快照。

提交前验证数量、明细汇总、账户盈亏、现金和权益守恒。数据库批次完成后，
活动订单索引按 PostgreSQL 重建；Redis 直接写入下一交易日基准快照：结算价
作为 mark，`daily_position_pnl`、`daily_close_pnl` 和现金口径浮盈均为 0，
不把旧交易日 Tick 作为重建触发。该过程不删除 Redis Stream、Consumer Group
Pending 或 Outbox。Redis 恢复失败时批次保持 `COMPLETED`，
`cache_status=FAILED`，再次运行只重试派生缓存恢复。

实时 Worker 的每次计算要求 Tick、Position、Account 的 `trading_day` 一致；
写入前还以持仓缓存版本执行 Redis Lua CAS。日终切换使版本递增，因此切换前
已开始的旧周期不能覆盖次日基准。后端统一返回累计净盈亏：

```text
累计净盈亏 = 累计已实现盈亏 + 当前全部持仓累计经济盈亏 - 累计手续费
```
