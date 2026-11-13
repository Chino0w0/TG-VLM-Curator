# Agent Handoff — TG-VLM-Curator

更新：2026-11-13 06:05 UTC。工作区：`E:/Project/TG-VLM-Curator`。M10 / #31 远端合并基线为 `f6ac697`（代码门禁 SHA `8fc2737`）；本轮 Post-M10 全栈加固交付分支 `fix/post-m10-full-stack-hardening` 的验证代码基线为 **`c5456dc`**（`c5456dc3756d62b3385aa19de7d69b5ae3e18a71`）。历史33项映射与冻结证据保留在 `docs/reviews/2026-11-12-remediation-status.md`（禁止改写历史审查报告）；架构依据为 `tg-vlm-curator-architecture.md`，模块边界为 `tg-vlm-curator-module-implementation.md`。

## 当前交付与授权

M0–M10 均已合并至 `main`（M6 / #27 `99696fa2`、M7 / #28 `28c3e6fd`、M8 / #29 `b0e955f4`、M9 / #30 `0007be1e`、M10 / #31 `f6ac697`）。在 `f6ac697` 之上，`fix/post-m10-full-stack-hardening` 依次 `--no-ff` 合并了四个独立审查通过的加固分支：
- `fix/final-core-hardening`（`24bd8ed`）：新增迁移 `b9c3e5f7a106`（`service_heartbeats`、热路径复合索引、`audit_events`/`publish_records` 追加只读触发器、相册重开清理陈旧 `duplicate_matches`）与 M6 `'deleting'` 降级归一；`VisualImageBackend` 移除应用层 `PIL` 直接依赖与可分离 32x32 DCT；`FFmpegVideoFrameExtractor` 异步取消立即终止子进程、MKV 时长回退、EOF 夹紧与单临时文件批量抽帧；视觉缓存 SHA-256 键、批次 `commit_cache_hit` 按位隔离、单目标 prepare 错误隔离、formal 多阶段 DAG 完成判定、`deleting`/`deleted` 媒体失败关闭；跨版本 `label_key` 人工覆盖与清理过滤、pHash 近重复清理判定、活跃占用锁与清理租约续期；`TelethonPublisher` 频道级 `noforwards`、纯文本 `copy_with_caption` 与机器人/跨身份重传，以及 `LocalArchiveStorage` NUL/目录符号链接逃逸防护。
- `fix/final-admin-api-hardening`（`8f71f06`）：全部 6 类 `step_kind` 失败查询与仅允许 `failed` 状态重试保护；跨进程 `service_heartbeats` 读取、SQL 重建发布/FloodWait 计数与全部 17 组 Prometheus 指标族；配置跨表引用与 `published` 状态校验、单事务 `initial_version` 回滚、`mtproto_user` 来源读取身份校验、`ProcessingRange` 终态与参数守卫、DTO 字段对齐、`ILIKE` 通配符转义、N+1 批量加载与 60 秒会话写节流；`SecretStr` + 422 脱敏异常处理器、`expected_secret_type`（`telegram_session`/`bot_token`/`api_key`）全链路隔离、`InferenceProfile.base_url` SSRF 校验（`TGCURATOR_ALLOW_LOCAL_INFERENCE` 默认关闭），以及用 `httpx.AsyncClient(ASGITransport)` 全面替换 `TestClient` 消除弃用警告。
- `fix/final-web-hardening`（`8b702a6`）：前后端 DTO 与枚举严格对齐、规范 `secret_type` 选择与按身份类型过滤、`parseTelegramPeerId` 与可选字段清空归一、未保存草稿切换拦截、请求代数与变更互斥锁、已结算消息禁用 Skip、无障碍 `ModalDialog`/`Tabs`、`role="search"` 消除测试 stderr 警告，以及 `React.lazy` 工作区按需分包。
- `fix/final-ops-runtime-hardening`（`18620b5`）：共用 `${COMPOSE_PROJECT_NAME:-tgcurator}-backend:local` 镜像、分层缓存与 `no-new-privileges` / 日志轮转；`Settings` URL 编码拼装 `POSTGRES_*` 支持特殊字符密码；`docker/nginx.conf` 端口 80 固定跳转 `TGCURATOR_SERVER_NAME`（默认 `localhost`，阻断伪造 `Host` 开放重定向）、安全响应头、缓存策略与 gzip；备份/恢复脚本 `restarting` 检查、SHA256 CRLF/大小写归一、`python3`/`python` 探测与一致性差异显式报错；守护进程可中断等待、磁盘+SQL 心跳，以及 Celery worker 进程级持久 `WorkerRuntime` / 连接池复用与 `max_concurrency` 信号量限流。
- 跨切片联调补充提交：`1290c20`（`pipeline_repository.repair_wakeups` 仅修复已授权且处于 `completed`/过期 `leased` 的 `pending` 媒体唤醒，保持 `defer_media_wakeups=True` 与 PreScreen Negative Gate 拦截不提前下载媒体；修正 `runtime_fixtures` 在 `fixed` 范围下不传 `steady_after_seconds`）与 `c5456dc`（`docker/backend.Dockerfile` 缓存依赖层显式安装 `hatchling>=1.25` 以支撑 `--no-build-isolation` 源码安装）。

本轮获授权通过远端 PR（`fix/post-m10-full-stack-hardening` -> `main`）提交审查并合并，严格以最新 head 在 GitHub Actions（`python` / `web` / `deployment`）三项门禁全绿及 `--match-head-commit` 为前置条件。未授权生产数据库、真实频道投递或外部推理验收。

## 已完成的修复范围

在保留 M6–M10 既有修复契约基础上，Post-M10 全栈加固补齐了跨进程运维遥测、管理员 API 全量引用/密文边界、前端契约与并发竞态守卫、媒体/缓存/DAG 边界隔离、容器构建缓存与反向代理重定向安全。

视觉输入通过异步 storage resolver 读取实际归档图片/帧，Manifest v2 绑定发送内容、次序和目标/区域；raw/contact_sheet/adaptive 执行真实预算，媒体缺失、损坏、已清理（`deleting`/`deleted`）或容量不足均失败关闭。冻结 Source Profile 的媒体版本实际控制图片规格与视频帧数，后续 active Profile 变更不能改写冻结执行。Compose 保持十服务、四 named volumes、只读 master-key 文件和零模型服务；HTTPS 保留 Secure cookie，备份恢复通过 Compose PG16/媒体 volume 与独立主密钥操作。

## 验证证据与准确边界

历史证据目录 `.git/fix-2026-11-12/` 不随普通 Git 提交分发。禁止把任何服务配置、环境文件或密钥复制到报告/日志/提交；可归档结果须先脱敏。以下为代码基线 **`c5456dc`** 的本机实测门禁结果：

| 验证 | 结果 / 定位 |
| --- | --- |
| 最终全量 Python | `pytest -ra -W error --durations=20`：**648 passed，0 failed，0 errors，0 skipped，0 warnings**（耗时 `236.62s`）。 |
| 原生集成 40 项 | 实时 PG16/Redis7 共 **40** 项：`M6=6`、`M7=6`、`M8=8`、`media_profile=6`、`post_m10_core_postgres=6`、`post_m10_admin_http_postgres=3`、`runtime_closed_loop=3`、`telegram_revocation=1`、`worker_process_restart=1`；另有 `alembic_offline_sql=18`（含 `b9c3e5f7a106`）。 |
| 原生浏览器 | `web/tests/native_postgres_runner.py`：真实 PG16 迁移 + FastAPI + SQL vault + Headless Chrome 生产 bundle，**17/17 组全过**，`errors=[]`，`database_removed=true`，`source_freeze_unchanged=true`。 |
| 前端 | `npm.cmd run typecheck` 通过；`vitest run` **33/33 passed** 且 **0 stderr 警告**；`vite build` 产出入口 `240.49 kB` + 12 个按需工作区 chunk；`npm.cmd audit --json` 漏洞总数 **0**。 |
| 独立 OS 重启 | `test_worker_process_restart.py`：未知远端 fixture 结果时强杀子进程、4 秒租约到期后新 worker PID 恢复、5 条记录对应 5 个唯一远端效果；包含在 648 全量零警告通过中。 |
| 完整部署演练 | `scripts/check-deployment.py` 退出 **0**（`147.32s`）：十服务构建/启动/重启、含特殊字符 `@:/?#%` 的 PG 密码、真实容器 `ffprobe`/抽帧/活跃 FFmpeg 取消、TLS Secure Cookie 登录与 `/auth/me`、伪造 `Host` 308 跳转阻断、`/metrics` 404、全部应用角色 `SIGTERM` 优雅退出、PG16+媒体卷+独立主密钥恢复、孤儿文件非零退出（exit `2`）与清理后干净扫描、错误密钥拒绝，且 **0 残留 smoke 镜像/容器/卷**。 |
| 静态与打包检查 | `ruff format --check .`（289 文件）、`ruff check .`、`python -m pip check`、`python -m compileall -q apps tgcurator migrations tests scripts`、Git Bash `bash -n scripts/*.sh` 全部退出 `0`。 |

原先 `fastapi.testclient.TestClient` 触发的两条 Starlette/anyio 弃用警告已通过迁移至生命周期感知的 `httpx.AsyncClient(ASGITransport)` 彻底消除，全量套件在 `-W error` 下零警告通过。

原生浏览器不运行清理 worker，60 个 cleanup items 仅验证入库，没有删媒体；Test Bench 使用 `preview_only`，不调用外部模型。手机布局仅抽查 Audit（`390px`），不扩称全页面/跨浏览器。闭环与独立重启测试中的 Telegram 及 HTTP 推理均为本地受控 fixture，不是真实频道投递或真实模型服务。

本次验证临时创建的全部 PG16/Redis7 容器、`tgcurator_test_*` 数据库、Redis DB15 键、两个 `tgcurator-ops-smoke-*` Compose project/volumes 及演练镜像均已 100% 清理并核查；宿主机原有容器、镜像与数据卷保持原状未触碰。

## 继续工作入口

运行配置见 `docs/operations/runtime.md`；TLS、备份与恢复见 `docs/operations/deployment.md`。关键代码为 `apps/worker/runtime.py`、`apps/telegram_ingestor/runtime.py`、`apps/scheduler/runtime.py`、`tgcurator/infrastructure/database/reanalysis_repository.py`、`pipeline_repository.py`、`admin_api_repository.py`、`tgcurator/application/analysis/input_composer.py`；可复跑入口为 `tests/integration/`、`web/tests/README.md` 和 `scripts/check-deployment.py`。

任何向远端 `main` 的 PR 合并须先确认最新 head 在 GitHub Actions（`python` / `web` / `deployment`）全部达到 `SUCCESS` 并使用 `--match-head-commit`。真实 Telegram 账号/session、测试来源/目的频道和真实外部推理 provider 需另有具体授权与配置；禁止用生产频道试发补验收。API readiness 只证明 PG，worker/scheduler readiness 证明 PG+Redis，不等于外部业务能力就绪。主密钥独立保存，恢复必须确认可解密和媒体一致，不将密钥放入数据库/媒体备份。

## 工作规则

遵循本机与项目 AGENTS 及现有模块边界，保留他人修改；PostgreSQL 是业务真相，Redis 为可重建 UUID 唤醒；不捆绑或部署模型。新验证必须标清代码 SHA、命令结果与 fixture/真实服务边界。历史审查报告（`docs/reviews/*`）保持原始冻结证据，当前状态只在交付文档同步。没有对应终态证据时，明确写待核验，不写“首版全栈生产完成”。
