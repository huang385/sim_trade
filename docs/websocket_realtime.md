# WebSocket实时交易推送联调说明

第一版Gateway是同仓库中的独立进程，只读取PostgreSQL事实、Redis快照和
`stream:realtime-events`，不会执行下单、撮合、结算或资金计算。

## 启动顺序

先完成数据库迁移并启动原有API和业务Worker，再启动实时投影与Gateway：

```powershell
alembic upgrade head
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
python -m app.workers.outbox_publisher_worker
python -m app.workers.realtime_event_projection_worker
python -m app.scripts.run_websocket_gateway
```

订单活动索引、行情、撮合和PnL仍按原有方式启动。需要看到实时盈亏和期权
估值时，必须同时运行`realtime_pnl_worker`。Gateway固定使用一个Uvicorn
Worker；第二个实例会因`ws:gateway:lease`租约而拒绝启动。

## 浏览器流程

1. HTTP登录取得短期Access Token；Refresh Token只保存在HttpOnly Cookie。
2. 调用`POST /api/ws/ticket`取得一次性Ticket。
3. 连接`ws://127.0.0.1:8001/ws/trading?ticket=...`。
4. 发送`{"action":"subscribe","account_ids":["A001"]}`。
5. 首条业务数据为`SNAPSHOT`，之后接收统一增量事件。
6. 收到`HEARTBEAT`时回复`{"action":"pong"}`。

本地测试页面为`http://127.0.0.1:8000/test-trading`。页面只在内存保存Access
Token，不显示完整Ticket；WebSocket断开后指数退避重连并重新加载快照。

## 事件与恢复

金额字段全部使用十进制字符串。事件中有两类互不混用的版本：

- `version`是`stream:realtime-events`中的Redis Stream消息编号，只用于断线
  恢复、快照屏障和传输顺序。
- `business_version`是PostgreSQL Outbox自增编号，用于判断同一个订单、成交、
  持仓或账户的业务事实新旧。

投影写入Redis时通过Lua脚本原子比较聚合根的`business_version`。晚到的旧
Outbox消息会被标记为已处理，但不会重新写入实时Stream，因此不能把
`FILLED`覆盖回`ACCEPTED`，也不能用旧账户或持仓值覆盖新值。

订阅时Gateway先注册临时账户路由并缓冲事件，再以PostgreSQL
`REPEATABLE READ + READ ONLY`事务读取事实快照，同时严格读取Redis实时PnL
快照。快照构建完成后会再次查询当前用户角色和账户归属，只有前后两次授权
均通过才发送`SNAPSHOT`，随后只补发高于快照游标的事件。因此账户转移、
管理员降权和订阅期间授权变化都不会形成越权或丢失窗口。严格快照所需的
Redis数据缺失时连接会要求重试，不会静默退回较旧的PostgreSQL实时值。

业务事务还会在同一个Outbox中生成绝对事实事件：

- `ACCOUNT_UPDATED`包含账户资金、保证金、手续费、盈亏和风险字段的绝对值；
- `POSITION_UPDATED`包含当前持仓数量、成本、保证金和盈亏绝对值；
- 持仓全部平完后发送`POSITION_CLOSED`，客户端直接移除该持仓。

浏览器收到成交事件后不再全量重新订阅，而是等待上述账户和持仓事实事件做
局部更新，避免成交高峰期间反复构建完整快照。

Gateway Consumer Group首次创建使用`$`，只消费启动后产生的新事件；历史
状态由首次完整快照提供。Gateway对Stream执行ACK前会通过Lua再次核对
`ws:gateway:lease`所有者，旧实例丢失租约后不能继续路由或确认消息，并会
主动关闭已有连接，客户端重新连接到新实例后从完整快照恢复。

队列或快照缓冲达到上限时，Gateway关闭慢连接，客户端必须重新连接并重新
订阅。Access Token到期后连接会收到`AUTH_EXPIRED`并关闭；页面通过Refresh
Cookie取得新Access Token后再申请新Ticket。

连接存续期间Gateway会周期性重新查询用户状态、最新角色和当前订阅账户的
归属。用户被禁用时连接立即关闭；管理员被降级或账户被转移时，相应账户会
被自动取消订阅。Ticket中的角色只用于建立连接身份，不作为长期授权缓存。

使用项目启动脚本时Uvicorn访问日志默认关闭，防止查询参数中的完整Ticket
进入日志。若直接运行Uvicorn，请同时传入`--no-access-log`。
