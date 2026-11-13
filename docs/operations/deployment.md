# HTTPS 部署与灾难恢复

本说明面向 Docker Engine 与 Compose v2。生产保持 `TGCURATOR_ENVIRONMENT=production`，会话 Cookie 必须为 Secure、HttpOnly、SameSite=Lax。只有 nginx 的 80/443 发布到宿主；PostgreSQL、Redis 和 API 不发布端口。80 除 `/nginx-health` 健康检查外均按显式配置的 `TGCURATOR_SERVER_NAME`（默认 `localhost`，绝不反射请求头中的伪造 `Host`）执行 308 重定向至 HTTPS，443 在 nginx 终止 TLS 并附带 HSTS、`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: same-origin` 与静态资源 gzip/缓存策略。API 显式禁用代理头信任，应用记录的客户端地址是容器代理地址；真实客户端地址以 nginx 日志为准，不信任用户提供的 X-Forwarded-For。公开入口显式阻断 `/metrics`（返回 `404`），监控应从受控内部网络访问 API。

## 操作员前提与首次启动

操作员必须准备与访问域名匹配、客户端信任、未过期的证书完整链及对应私钥。设置 `.env` 中 `TGCURATOR_SERVER_NAME`（与证书域名一致）、`TGCURATOR_TLS_CERT_HOST_PATH` 和 `TGCURATOR_TLS_KEY_HOST_PATH`；证书与私钥文件必须真实存在，Compose 禁止自动创建同名目录，以只读挂载交给 nginx。没有公网域名不影响本地隔离验证，但自签测试证书不代表生产信任。证书续期后执行 `docker compose exec web nginx -t` 并重新创建 web，使替换后的 bind 文件生效。避免打印私钥、主密钥、密码或未脱敏的 `docker compose config`。

复制 `.env.example` 为 `.env`，选择明确的 `COMPOSE_PROJECT_NAME`，填写强随机 `POSTGRES_PASSWORD`（`Settings` 在未显式覆盖 `TGCURATOR_DATABASE_URL` 时会对 `POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_HOST`/`POSTGRES_PORT`/`POSTGRES_DB` 自动做安全百分号编码，支持特殊字符密码），将 `.env`、主密钥与证书目录限制为受控操作员可访问。7 个后端 Python 服务共用 `${COMPOSE_PROJECT_NAME:-tgcurator}-backend:local` 镜像并启用 `no-new-privileges:true` 与 `json-file`（`20m` x `5`）日志轮转。主密钥为 base64 编码的随机 32 字节，绝不写入 Git、数据库或备份包。Docker 的文件 secret 需要容器 UID 10001 能读取；Linux 上可将主密钥归属 UID 10001、权限 0400，并以 0700 的宿主父目录保护路径，不能用 root:root 0400 使非 root 应用无法读取。Windows 使用 ACL 限制宿主目录，并验证 Docker Desktop 的实际挂载读取能力。

```bash
cp .env.example .env
mkdir -p secrets/tls
python -c "from pathlib import Path; import base64,secrets; Path('secrets/app_master_key.txt').write_bytes(base64.b64encode(secrets.token_bytes(32)))"
# 填写 .env；将操作员证书/私钥放到配置路径；落实文件权限与独立离线主密钥保管。
export COMPOSE_PROJECT_NAME=tgcurator
export COMPOSE_ENV_FILE="$PWD/.env"
docker compose --env-file "$COMPOSE_ENV_FILE" build api web
docker compose --env-file "$COMPOSE_ENV_FILE" up -d --wait postgres redis
docker compose --env-file "$COMPOSE_ENV_FILE" run --rm api alembic upgrade head
docker compose --env-file "$COMPOSE_ENV_FILE" run --rm api tgcurator-bootstrap-admin --username curator
docker compose --env-file "$COMPOSE_ENV_FILE" up -d --build --wait
```

通过 `https://<证书域名>/` 登录，检查浏览器不出现证书警告，登录后 `/api/v1/auth/me` 成功，Cookie 带 Secure。防火墙仅允许必要的管理来源访问 443。若映射非标准 HTTPS 端口，同时设置 `TGCURATOR_HTTPS_PORT` 与 `TGCURATOR_HTTPS_REDIRECT_PORT`，使 80 重定向到正确的客户端可达端口。公开 HTTP 仅作为跳转入口，不可作为登录入口。

## 一致性备份

运维脚本在宿主 Bash 执行，依赖 Docker Compose、Python 3、sha256sum、tar、mktemp；Windows 使用 Git Bash 并先安装 Python。所有脚本强制要求显式导出 `COMPOSE_PROJECT_NAME`，通过 `COMPOSE_ENV_FILE` 指定配置，不会把 `.env` 当 shell 执行。自定义单一 Compose 文件可设置 `COMPOSE_FILE`。不要将多个路径拼接到这个变量。

备份期间停止所有应用写入方，并维持整个数据库、媒体备份的一致维护窗口。脚本同时检查 `running` 与 `restarting` 状态，拒绝存在运行中或重启退避中的应用服务；PG 和 Redis 可以保持启动。也必须停止外部写入客户端；脚本无法识别 Compose 外的数据库连接。备份不是跨存储在线快照。

```bash
export COMPOSE_PROJECT_NAME=tgcurator
export COMPOSE_ENV_FILE="$PWD/.env"
docker compose stop web api telegram-ingestor scheduler worker-media worker-analysis worker-publish worker-maintenance
bash scripts/backup-postgres.sh
bash scripts/backup-media.sh
docker compose up -d --wait
```

数据库备份通过 `compose exec -T postgres pg_dump` 使用 server 同镜像的 PostgreSQL16 客户端，不使用宿主 PG15，也不要求暴露5432。媒体备份通过 operations profile 的 `archive-tools` 挂载本项目 `archive-data` named volume。每份产物有 SHA256 sidecar 和 UTC/大小/独立密钥提示的 JSON 元数据。文件默认仅操作员可读。备份失败返回非零；只接受有完整 sidecar 的产物。校验和用于损坏检测而非来源认证，需通过独立可信渠道保管元数据，并按保密等级加密备份。

## 恢复到隔离项目

恢复先在新 project 演练，不对生产 project 执行。准备独立恢复 `.env`，全新数据库与空 `archive-data`，以及从离线密钥保管处单独取得的原主密钥（保持 key ID）。不要为恢复随机生成另一把主密钥，也不要把密钥放进数据库/媒体备份包。应用镜像应与备份 schema 匹配；升级迁移在完成恢复验证之后独立安排。

```bash
export COMPOSE_PROJECT_NAME=tgcurator-recovery-20261112
export COMPOSE_ENV_FILE="$PWD/recovery.env"
# recovery.env 设置独立密码、端口、恢复主密钥及证书路径。
docker compose --env-file "$COMPOSE_ENV_FILE" build api
docker compose --env-file "$COMPOSE_ENV_FILE" up -d --wait postgres
RESTORE_REPORT=./restore-consistency.json bash scripts/restore-postgres.sh \
  --confirm-restore ./backups/postgres/<dump>.dump \
  --media ./backups/media/<archive>.tar.gz
```

`--yes-restore` 只作为旧别名兼容，新命令统一使用 `--confirm-restore`。脚本先验证两份 SHA256、媒体路径和成员类型，再检查维护窗口及空媒体卷。数据库在单个事务中 `pg_restore --clean --if-exists --exit-on-error`，SQL 失败回滚并退出；媒体恢复失败时数据库可能已完成恢复，必须保持应用停止，调查后重新准备隔离目标，不自动删除或覆盖已有媒体卷。恢复不启动任何业务 worker。

随后强制运行真实数据库的 `apps.worker.consistency_cli --verify-sha256 --json`，报告持久化到 `RESTORE_REPORT`；缺失、损坏或孤儿媒体使脚本向 `stderr` 打印失败退出码与 `RESTORE_REPORT` 定位路径并返回非零。扫描器检查数据库与媒体关系，不能证明所有加密凭据可解密。正式切换前必须额外用独立主密钥验证至少一条恢复后的加密 Secret、管理员登录、业务记录及关键媒体，并人工处置差异。不要通过关闭一致性扫描或忽略退出码宣布恢复成功。生产切换、DNS 修改及生产旧卷删除均需单独授权。

## 可重复本地 TLS 与恢复演练

```bash
python -m pip install -e '.[runtime,dev]'
python scripts/check-deployment.py
```

该命令仅新建 UUID 命名的 `tgcurator-ops-smoke-*` 与 `*-restore` 两个 project，绑定 127.0.0.1 随机端口，使用临时目录、临时 PostgreSQL 密码、临时管理员、专属主密钥，以及一天有效的 localhost 自签证书。证书只被本次 HTTP 客户端显式信任，不导入系统信任库，不关闭 TLS 校验，并验证默认信任会拒绝证书。它使用 production 配置构建真实 API/前端镜像、执行真实 migrations，经 nginx443 完成登录和 `/auth/me`，验证 Secure/HttpOnly Cookie 和 HTTP308。

演练随后备份真实 PG16 与非空 named archive volume，将数据库和媒体恢复到另一个新项目，使用独立保存的密钥副本验证 Secret 解密，并确认错误密钥不能解密。刻意放置的孤儿媒体必须令恢复扫描返回非零且报告差异；验证媒体字节后，仅删除本次隔离夹具，再执行干净扫描。这个负向分支是演练的一部分，不意味着可以忽略正式恢复中的孤儿文件。该演练不调用 Telegram 或外部推理服务，也不证明业务 runtime 接线完整。

无论成功或失败，脚本都清理自己创建的两个 project、容器与卷；不会读取生产 `.env` 或使用集中测试服务。演练自己的镜像标签也会清理；构建缓存及基础镜像由 Docker 正常缓存管理。脚本拒绝 SSH/TCP 远程 Docker endpoint，只允许本地 Unix socket 或 npipe。不要在并行修改源码期间把单次演练结果当作最终合并验收。

## CI 门禁

`.github/workflows/ci.yml` 配置 PG16 和 Redis7 service containers，显式传入 `TGCURATOR_TEST_POSTGRES_ADMIN_URL` 与 `TGCURATOR_TEST_REDIS_URL`。pytest 使用既有 `tests/integration/postgres_support.py` 建立 UUID 临时数据库并运行真实迁移与仓储回归；没有显式服务变量时，本地真实 PostgreSQL 测试按既有 helper 跳过，不暗连生产。集中本地测试可以显式设置 `TGCURATOR_TEST_SERVICES_FILE`，不得打印其中凭据。

Python 门禁包括依赖检查、ruff lint/format、compileall 和 pytest；前端存在 `web/package.json` 时执行 npm ci、typecheck、test、build，无前端的分支条件跳过前端与完整部署演练。部署 job 独立运行上述 TLS/恢复演练。服务容器已就绪不等于真实 Redis 行为已覆盖；队列/runtime 回归由对应模块测试提供。云端 GitHub Actions 状态只能在实际推送后确认，本地通过不冒充远程 CI 已通过。
