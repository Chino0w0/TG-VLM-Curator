# Agent Handoff — TG-VLM-Curator

更新：2026-11-12 11:53 UTC。工作区：`E:/Project/TG-VLM-Curator`。本轮代码基线：`8fc2737`。详细33项映射、证据与限制见 `docs/reviews/2026-11-12-remediation-status.md`；架构依据为 `tg-vlm-curator-architecture.md`，模块边界为 `tg-vlm-curator-module-implementation.md`。

## 当前交付与授权

M0–M5为已有合并实现。M6 / #27已合并 `99696fa2`，M7 / #28已合并 `28c3e6fd`，M8 / #29已合并 `b0e955f4`，M9 / #30已合并 `0007be1e`。M10交付PR为#31，base为main，代码head `8fc2737` 已通过全部GitHub CI；文档最终head须按同一门禁并使用match-head-commit合并。实际merge commit读取远端PR记录，不从冻结SHA推导。用户已授权修复、测试、提交、推送和按门禁合并；未授权生产数据库、真实频道投递或外部推理验收。

本机pytest为596 passed、2 warnings，JUnit tests596/errors0/failures0/skipped0，耗时281.558秒；完整Docker部署退出0。GitHub PR run `43212224146` 与push run `43212215720` 的python/web/deployment全部SUCCESS，远端596 passed、1 warning、127.86秒。首轮依赖漂移引发的nullable JSON单测已用生产asyncpg dialect修正，并分别验证SQLAlchemy2.0.52与2.1.8。新任务先读取当前main与PR最终状态，不套用历史所有PR保持open的限制。

## 已完成的修复范围

M6修复非空清理父子写入顺序、带历史意图的迁移回填及触发器恢复、受保护内容首次/恢复fallback；发布人工retry仅重置本轮预算，审计attempt编号持续递增。M7修复实际迁移约束名、同秒canonical环、跨窗口媒体组revision、formal首次创建并发和重叠范围结算；无法证明当前语义等价的donor结果不直接复制，而进入正常worker/cache key路径。

M8真实SQLAlchemy配置创建与更新、消息筛选、SET/CLEAR有效事实、失败父run恢复和完整reanalysis DAG已修复。独立reanalysis不依赖旧ProcessingRange，不改旧formal历史，不自动reroute/publish。原生浏览器追加发现的cleanup estimate/confirm DTO也已修复。M9十项历史问题、初始配置schema/引用、清理竞态、安全确认、详情DTO、分页、MEDIA身份、dirty发布、注销/会话及必要管理入口已有回归。

视觉输入通过异步storage resolver读取实际归档图片/帧，Manifest v2绑定发送内容、次序和目标/区域；raw/contact_sheet/adaptive执行真实预算，媒体缺失、损坏、容量不足失败关闭。冻结Source Profile的媒体版本实际控制图片规格与视频帧数，后续active Profile变更不能改写冻结执行。显式缺失/冲突配置不会静默回退；旧READY资产不会自动重写。

生产组合入口从vault和已发布版本连接Telegram历史、实时订阅/补抓、媒体、HTTP推理及发布，身份停用会撤销客户端和排队旧事件。Compose保持十服务、四named volumes、只读master-key文件和零模型服务；HTTPS保留Secure cookie，备份恢复通过Compose PG16/媒体volume与独立主密钥操作。

## 验证证据与准确边界

证据根目录 `.git/fix-2026-11-12/` 不随普通Git提交分发。禁止把其中的服务配置、任何环境文件或密钥复制到报告/日志/提交；可归档的结果应先脱敏。

| 验证 | 结果 / 定位 |
| --- | --- |
| 最终全量 | `main/final-full.txt`、`main/final-full.xml`：596 passed，0 skip/failure/error。 |
| 原生集成31项 | M6=6、M7=6、M8=8、media_profile=6、runtime_closed_loop=3、telegram_revocation=1、worker_process_restart=1；另有offline_sql=17。 |
| 原生浏览器 | `main/pr30-native-independent/`：真实PG16、FastAPI、SQL vault、Chrome生产bundle，17组全过，无DTO回放或InMemory。 |
| 前端 | 24 Vitest、typecheck/build通过；`main/web-audit.json`漏洞总数0。 |
| runtime | `runtime/fixed.json`、`latest.json`、`final-run.txt`：真实PG/Redis/Celery、生产factory及本地外部fixture。 |
| 独立OS重启 | `runtime/restart-run.txt`、`restart-latest.json`：未知远端fixture结果时kill、4秒租约、新worker PID恢复、5条记录/5个唯一效果；包含在最终596中。 |
| 完整部署 | `main/final-deployment.txt`退出0：十服务启动重启、真实ffprobe/frame/FFmpeg取消、TLS登录、应用SIGTERM、PG16+media+独立key恢复、orphan错误与clean scan、wrong-key、自有两个project/卷清理。 |
| 静态检查 | Ruff282文件、pip check、compileall通过；asyncpg兼容单测增量 `8fc2737` 在2.0.52/2.1.8均通过。 |

中间590/5失败已修复并由最终全量取代：E2E使用有效真实图像fixture，M7离线Alembic测试改用隔离Config避免fileConfig禁用其他worker logger，诊断断言保留。不要删除断言来“修复”日志捕获。两条弃用警告保留，不能称零警告。

原生浏览器不运行清理worker，60个cleanup items仅验证入库，没有删媒体；Test Bench使用preview_only，不调用外部模型。注销503故障注入在早期InMemory真实HTTP/Vitest中验证，原生模式只验证过期与正常注销。手机仅抽查Audit，不扩称全页面/跨浏览器。独立重启测试的Telegram效果为持久fixture，不是真实频道投递。

runtime专属数据库与Redis键已实时核查为空；独立子进程、浏览器及HTTP harness按各自清理证据退出。Docker演练已清理其自有两个project/卷。共享测试PG/Redis容器由主代理统一决定保留或关闭，不删除别的任务资源。

## 继续工作入口

运行配置见 `docs/operations/runtime.md`；TLS、备份与恢复见 `docs/operations/deployment.md`。关键代码为 `apps/worker/runtime.py`、`apps/telegram_ingestor/runtime.py`、`tgcurator/infrastructure/database/reanalysis_repository.py`、`pipeline_repository.py`、`admin_api_repository.py`、`tgcurator/application/analysis/input_composer.py`；可复跑入口为 `tests/integration/`、`web/tests/README.md` 和 `scripts/check-deployment.py`。

接续工作先读取当前main、#31最终head的GitHubCI与mergeCommit；本文件记录的是明确代码基线，不替代实时Git状态。真实Telegram账号/session、测试来源/目的频道和真实provider需另有具体授权与配置；禁止用生产频道试发补验收。API readiness只证明PG，worker/scheduler readiness证明PG+Redis，不等于外部业务能力就绪。主密钥独立保存，恢复必须确认可解密和媒体一致，不将密钥放入数据库/媒体备份。

## 工作规则

遵循本机与项目AGENTS及现有模块边界，保留他人修改；PostgreSQL是业务真相，Redis为可重建UUID唤醒；不捆绑或部署模型。新验证必须标清代码SHA、命令结果与fixture/真实服务边界。历史审查报告保持原始证据，当前状态只在交付文档同步。没有对应终态证据时，明确写待核验，不写“首版全栈生产完成”。
