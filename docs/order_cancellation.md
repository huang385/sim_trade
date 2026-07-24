# 限价开仓订单主动撤单

本阶段只支持 `LIMIT + OPEN` 订单撤销，不开放平仓、撤持仓或盈亏结算。

## 状态转换

- `ACCEPTED → CANCELLED`：全部剩余数量转入 `cancelled_volume`。
- `PARTIALLY_FILLED → PARTIALLY_CANCELLED`：保留已成交数量和持仓，只撤销剩余数量。
- `CANCELLED`、`PARTIALLY_CANCELLED`：重复撤单幂等返回。
- `FILLED`、`REJECTED`、`NEW`：返回 `ORDER_NOT_CANCELLABLE`。

订单始终满足：

```text
total_volume = traded_volume + remaining_volume + cancelled_volume
```

## 资金释放

撤单直接释放订单数据库中剩余的 `frozen_margin` 和
`frozen_commission`，不重新按价格或费率计算。释放只增加
`available_cash` 并减少账户冻结字段，不修改现金余额、权益、实际保证金、
实际手续费、成交和持仓。

## 事务和并发

撤单与成交使用相同锁顺序：

```text
Order → Account → Position（仅成交）
```

谁先获得 Order 行锁谁先完成。后获得锁的一方必须根据数据库最新状态决定
撤销剩余量、部分撤销或拒绝撤销，因此不会超量成交或重复释放资金。

## Outbox 与 Redis

订单、账户和 `ORDER_CANCELLED` / `ORDER_PARTIALLY_CANCELLED` Outbox
在一个 PostgreSQL 事务提交。API 不访问 Redis。发布 Worker 把事件发送到
`stream:orders`，订单事件 Consumer 再根据 PostgreSQL 最新终态原子删除
活动订单 Hash、合约 Set、账户 Set 和全局 Set。

Redis 暂时不可用不会回滚已经成功的撤单；Outbox 恢复后继续补发。
