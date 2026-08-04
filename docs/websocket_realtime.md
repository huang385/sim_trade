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

金额字段全部使用十进制字符串。客户端以目标实时Stream消息编号作为
`version`，忽略不大于本地版本的旧事件。订阅时Gateway先注册账户路由并
缓冲事件，再读取完整快照，随后只补发高于快照游标的事件，因此不存在
“读取快照后、开始订阅前”的丢失窗口。

队列或快照缓冲达到上限时，Gateway关闭慢连接，客户端必须重新连接并重新
订阅。Access Token到期后连接会收到`AUTH_EXPIRED`并关闭；页面通过Refresh
Cookie取得新Access Token后再申请新Ticket。

使用项目启动脚本时Uvicorn访问日志默认关闭，防止查询参数中的完整Ticket
进入日志。若直接运行Uvicorn，请同时传入`--no-access-log`。
