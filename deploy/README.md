# sim_trade 单机 Linux 生产部署

本方案在一台 Linux 服务器上运行 PostgreSQL、Redis、API、用户注册门户、单实例 WebSocket Gateway、全部常驻 Worker，以及相邻目录中的 reference-sync 定时同步器。为让本机服务互通，API、注册门户、行情订阅 Worker 和 reference-sync 使用 host 网络；生产公网流量应由宿主机 Nginx 提供 HTTPS。所有命令均在仓库根目录执行，且不会要求宿主机安装 Python 包。

完整上线顺序是：

1. 安装 Docker Engine 和 Docker Compose 插件。
2. 上传两个私有 SDK wheel 到 `private-wheels/`。
3. 从 `.env.production.example` 创建 `.env.production`，填写配置并设为权限 `600`；同时保护同级 `user_registration/.env` 中的 SMTP 与门户服务管理员凭据。
4. 执行 `docker compose --env-file .env.production build`。
5. 先启动 PostgreSQL 与 Redis，并等待健康检查通过。
6. 执行数据库迁移。
7. 执行活动订单索引重建。
8. 启动全部服务。
9. 检查 `docker compose ps` 和各服务日志。
10. 检查 API `/health`、Gateway `/health` 与注册门户 `/api/health`。
11. 配置宿主机 Nginx 与 TLS 证书。
12. 验证 SDK 导入、行情订阅、下单、撤单、撮合、实时 PnL 和 WebSocket 推送。
13. 配置 PostgreSQL 备份、日志轮转、监控告警，并复核容器重启策略。

## 部署前准备

1. 按服务器发行版的 Docker 官方文档安装 Docker Engine 与 Docker Compose 插件，确认 `docker --version` 和 `docker compose version` 可用。将部署用户加入 Docker 组等同授予高权限，应限制该账号并重新登录使组权限生效。
2. 在仓库根目录创建或使用 `private-wheels/`，手工上传且仅保存在服务器本地：

   - `ymm_live_data_sdk-0.8.5-*.whl`
   - `ymm_data_sdk-0.9.4-*.whl`

   wheel 已被 Git 忽略，禁止提交、上传到公共制品库或复制到公开目录。构建上下文会将它们交给本机 Docker 构建器，但多阶段构建不会把原始 wheel 复制进最终镜像层。应确认 wheel 的 Python ABI、CPU 架构与 Linux 服务器匹配。

   reference-sync 必须位于 `sim_trade` 的相邻目录 `../reference-sync`。它的镜像通过 BuildKit 私有附加上下文读取 `sim_trade/private-wheels/ymm_data_sdk-0.9.4-*.whl`；原始 wheel 不会复制到 reference-sync 目录或最终镜像。用户注册门户同样必须位于相邻目录 `../user_registration`，其 `.env` 与 `.data` 不进入镜像。

3. 创建生产配置并收紧权限：

   ```bash
   cp .env.production.example .env.production
   chmod 600 .env.production
   ```

   至少必须填写 `POSTGRES_PASSWORD`、`AUTH_JWT_SECRET`、`API_CORS_ALLOWED_ORIGINS`，以及两套 Live 行情凭证：`FUTURES_MARKET_DATA_API_USER`、`FUTURES_MARKET_DATA_API_TOKEN`、`SECURITIES_MARKET_DATA_API_USER`、`SECURITIES_MARKET_DATA_API_TOKEN`。Data SDK 使用独立的 `YMM_DATA_SDK_MODE=local`，在 SDK 服务器本机不需要 `YMM_DATA_SDK_TOKEN`；只有把 Data SDK 改为 `lan` 或 `TS` 时才填写该 Token。JWT Secret 应由安全随机源生成且至少 32 字节，不要写入命令历史、Git 或聊天记录。`AUTH_REFRESH_COOKIE_SECURE=true` 与 `DEBUG=false` 必须保持不变。

   `REMOTE_MARKET_DATA_MODE` 只控制两条 Live SDK 连接；`YMM_DATA_SDK_MODE` 只控制 Data SDK。期货域与证券域的 Live Token 不得互换或留空，`YMM_DATA_SDK_TOKEN` 则是 Data SDK 在 `lan`/`TS` 下的独立凭证。Live SDK 即使使用 `local` 仍需要各自的策略 Token。

   `api`、`futures-market-data` 和 `securities-market-data` 使用 host 网络，因此 Compose 会把它们的数据库连接覆盖为 `127.0.0.1:15432` 和 `127.0.0.1:16379`。独立端口可避免与 SDK 服务器已有的 PostgreSQL 冲突；其他 Worker 仍通过隔离的 bridge 网络使用服务名 `postgres`、`redis` 和标准端口。不要把 `POSTGRES_BIND_ADDRESS`、`REDIS_BIND_ADDRESS` 或 `API_BIND_ADDRESS` 改为 `0.0.0.0`，修改发布端口时也必须保持 Compose 映射与 host 网络服务配置一致。

   reference-sync 的 RQData license 单独保存在 `../reference-sync/.env`，该文件必须为权限 `600`。Compose 后加载 `.env.production` 获取当前主库账号，并强制覆盖 `POSTGRES_HOST=127.0.0.1`、发布端口、`REFERENCE_PROVIDER=YMM`、`YMM_DATA_MODE=local` 和空 Data SDK Token。RQData license 与 YMM Data SDK Token 是不同凭证；即使 Data SDK 的 `local` 模式无需 Token，YMM 缺失接口的回退仍可能需要 RQData license。

4. 如接入方要求私有 CA，把 CA 文件保存为 `deploy/certs/market-data-ca.pem`，权限设为只允许部署管理员读取，然后：

   ```bash
   cp deploy/compose.ca.yml.example deploy/compose.ca.yml
   ```

   同时设置 `REMOTE_MARKET_DATA_CA_FILE=/run/secrets/market-data-ca.pem`，后续每条 Compose 命令都额外添加 `-f deploy/compose.ca.yml`。override 只向使用 Live SDK 的行情订阅 Worker 只读挂载 CA。未配置该变量时不要创建 override，也不要挂载 CA。CA 目录和本地 override 已被 Git 忽略；也可由配置管理系统安全放置。

## 首次部署顺序

以下命令严格对应生产上线顺序。带私有 CA 时，为 Compose 命令追加 `-f deploy/compose.ca.yml`。

1. 构建统一应用镜像：

   ```bash
   docker compose --env-file .env.production build
   ```

2. 先启动并等待 PostgreSQL 与 Redis 健康：

   ```bash
   docker compose --env-file .env.production up -d --wait postgres redis
   ```

3. 新环境首次部署直接执行数据库迁移。脚本只运行 `alembic upgrade head`：

   ```bash
   ./deploy/scripts/migrate.sh
   ```

4. 从 PostgreSQL 重建 Redis 活动订单索引：

   ```bash
   ./deploy/scripts/rebuild-active-orders.sh
   ```

5. 启动 API、单实例 Gateway、全部 Worker 和 reference-sync：

   ```bash
   docker compose --env-file .env.production up -d
   ```

6. 检查容器与日志，不要在日志中粘贴 Token：

   ```bash
   docker compose --env-file .env.production ps
   docker compose --env-file .env.production logs --tail=200 api websocket-gateway
   docker compose --env-file .env.production logs --tail=200 futures-market-data securities-market-data futures-matching securities-matching realtime-pnl
   docker compose --env-file .env.production logs --tail=200 reference-sync
   ```

7. 检查 API 与 Gateway 健康端点：

   ```bash
   ./deploy/scripts/health-check.sh
   ```

   也可从宿主机检查 `http://127.0.0.1:8000/health` 和 `http://127.0.0.1:8001/health`。Gateway 结果中的 `single_instance_lease` 必须为 `true`。

8. 复制 `deploy/nginx/sim-trade.conf.example` 到宿主机 Nginx 配置目录，替换域名与证书路径，执行 `nginx -t` 成功后再 reload。证书和私钥只保存在宿主机安全位置，不放进镜像或 Git。
9. 经 HTTPS/TLS 逐项验证：两个 SDK 能导入、行情订阅正常、下单与撤单闭环、撮合结果正确、实时 PnL 更新、WebSocket `/ws/trading` 推送与重连正常。先使用受控账户和最小业务范围验证，再切换正式流量。
10. 设置 PostgreSQL 定期备份与恢复演练；确认 Docker `json-file` 日志上限适合磁盘容量；监控磁盘、内存、容器健康和重启次数。Compose 已为业务容器、PostgreSQL 和 Redis 设置 `restart: unless-stopped`，但它不能替代告警与数据备份。

## 更新与日常操作

只停止 API、Gateway、全部 Worker 和 reference-sync，同时保留 PostgreSQL、Redis 及其数据：

```bash
./scripts/stop_all.sh
```

重新启动全部业务服务（脚本会先验证 PostgreSQL 和 Redis 已经健康，但不会启动或重建它们）：

```bash
./scripts/start_all.sh
```

两个脚本默认读取 `.env.production`。如使用其他环境文件，可显式传入 `SIM_TRADE_ENV_FILE=/绝对路径/配置文件`。如存在按文档启用的 `deploy/compose.ca.yml`，脚本会自动叠加该文件。这里使用的是容器级优雅停止，不要用 `docker compose pause` 冻结租约和网络连接。

代码或依赖更新后，先备份并审阅变更，再执行：

```bash
docker compose --env-file .env.production build
./deploy/scripts/migrate.sh
docker compose --env-file .env.production up -d
docker compose --env-file .env.production ps
./deploy/scripts/health-check.sh
```

查看单个 Worker 日志示例：

```bash
docker compose --env-file .env.production logs -f --tail=200 risk-monitor
```

不要通过 `--scale websocket-gateway=2` 扩容 Gateway；当前实现固定为单实例。资源默认值只是起点，应通过 `.env.production` 的 `APP_CPUS`、`APP_MEMORY_LIMIT`、`POSTGRES_MEMORY_LIMIT`、`REDIS_MEMORY_LIMIT` 和日志上限按压测结果调整。

reference-sync 本身已经包含上海时区的常驻日内调度，不要再为它配置 cron 或 systemd timer。其状态和日志保存在 `reference-sync-runtime`、`reference-sync-logs` 命名 volume；查看计划任务日志使用：

```bash
docker compose --env-file .env.production logs -f --tail=200 reference-sync
```

调度内容包括每日交易日历、盘前合约与规则、盘后现金证券事实，以及夜盘前下一交易日规则。它和“人工日终结算”不是同一程序；交易结算仍必须按下一节人工确认交易日后执行。

## 人工日终结算

日终不是常驻 Worker，也不能按自然日无条件定时执行。只有在人工确认目标日期确为交易日、当日行情完整、上游停止或状态满足业务要求后，才运行：

```bash
TRADING_DAY=YYYY-MM-DD
docker compose --env-file .env.production run --rm api \
  python -m app.scripts.run_daily_settlement --trading-day "$TRADING_DAY"
```

`YYYY-MM-DD` 必须在每次执行前人工替换并复核。可将 `deploy/systemd/sim-trade-settlement@.service.example` 复制到 `/etc/systemd/system/sim-trade-settlement@.service`，替换部署用户名和仓库绝对路径并执行 `systemctl daemon-reload`，之后由两人复核交易日再人工触发：

```bash
sudo systemctl start sim-trade-settlement@YYYY-MM-DD.service
sudo journalctl -u sim-trade-settlement@YYYY-MM-DD.service
```

模板故意不提供 Timer，防止周末、节假日或异常交易日被自然日计划误触发。
若启用了私有 CA，还必须在 systemd 模板的 Compose 参数中加入 `-f deploy/compose.ca.yml`，安装后用 `systemd-analyze verify` 检查 unit。

## 从旧系统迁移真实数据

首次部署空环境时，直接按前述顺序执行迁移即可。若迁移真实数据，必须安排停机窗口并遵守以下顺序：

1. 在旧系统停止 API，以及所有可能写入订单、成交、行情、PnL 的 Worker，确认没有残留写入进程和未完成事务。
2. 对旧 PostgreSQL 做一致性备份，记录版本、时间和校验值；保留原备份，不在原库上实验恢复。
3. 在新 PostgreSQL 创建一个明确命名的空目标数据库并导入，随后执行 Alembic 迁移。
4. 执行活动订单索引重建。
5. 核对账户、订单、成交、持仓以及关键资金汇总；核对通过后才允许 Nginx/上游切流。

Redis 不是权威迁移数据源，不应把旧 Redis 当作数据库备份恢复。允许新环境冷启动后由 Worker 重建缓存、活动订单索引和 Stream 消费状态；切流前必须观察各 Consumer Group 与死信流。

安全的 PostgreSQL 自定义格式备份示例（只读取数据库，输出到宿主机新文件）：

```bash
umask 077
docker compose --env-file .env.production exec -T postgres sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' \
  > sim_trade_YYYYMMDD.dump
```

恢复时先人工选择一个不存在的新库名。`createdb` 在目标已存在时会失败，以下命令不包含删除数据库或 `--clean`：

```bash
RESTORE_DB=sim_trade_restore_YYYYMMDD
docker compose --env-file .env.production exec -T \
  -e RESTORE_DB="$RESTORE_DB" postgres sh -c \
  'createdb -U "$POSTGRES_USER" "$RESTORE_DB"'
docker compose --env-file .env.production exec -T \
  -e RESTORE_DB="$RESTORE_DB" postgres sh -c \
  'pg_restore -U "$POSTGRES_USER" -d "$RESTORE_DB" --exit-on-error --no-owner' \
  < sim_trade_YYYYMMDD.dump
```

执行恢复前再次确认 `$RESTORE_DB` 是专用空库。不要对现有生产库使用 `dropdb`、`--clean`、覆盖式恢复或自动删除 volume。

## 停止与数据保护

正常停止容器使用：

```bash
docker compose --env-file .env.production stop
```

不要在生产机使用 `docker compose down -v`，它会删除命名数据卷。普通 `down` 不删除命名卷，但执行前仍应确认备份和命令目标。数据库数据位于命名卷 `postgres-data`，Redis AOF 位于 `redis-data`，同步器状态与日志位于 `reference-sync-runtime` 和 `reference-sync-logs`；这些内容均不进入镜像或 Git。
