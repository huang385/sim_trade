# 股票现金订单受理（二阶段）

接口：

- `POST /api/stock/orders`
- `POST /api/stock/orders/{order_id}/cancel`
- `GET /api/stock/orders/{order_id}/fee-components`

股票请求不接受 `offset_flag`，且仅支持 `LIMIT`。服务端依据交易日、`StockTradingRule`、
`StockDailyTradingFact` 和 Instrument 校验请求；不会信任客户端传入的用户、合约类型或交易日。

`STOCK_ORDER_ENTRY_ENABLED` 默认是 `false`，显式开启后才受理外部股票订单。
`STOCK_MATCHING_ENABLED` 在本阶段保持 `false`；股票 Outbox 事件会被确认，但不会进入活动订单索引、
衍生品撮合或成交结算链路。

买单冻结：

```text
order_amount = limit_price * volume * contract_multiplier
required_cash = order_amount + estimated_buy_fee

available_cash -= required_cash
frozen_cash += order_amount
frozen_commission += estimated_buy_fee
```

卖单不冻结现金；它仅冻结 LONG 持仓：

```text
frozen_volume += volume
available_volume = total_volume - frozen_volume - settlement_locked_volume
```

同一 `account_id + client_order_id` 是幂等键。授权校验在幂等结果返回之前完成；相同键但请求内容不同返回冲突。
订单、资金或持仓冻结、手续费组件快照和 Outbox 事件均在同一 PostgreSQL 事务中提交。

`fee_rule_item` 的股票规则要求 `instrument_type=STOCK`、`offset_flag=NULL`，可配置多个
`fee_type`。每个组件独立按金额或数量计算，独立量化并应用 `minimum_fee`，之后再求和。
订单受理时写入 `order_fee_component_snapshot`，后续规则变更不会影响已受理订单。
