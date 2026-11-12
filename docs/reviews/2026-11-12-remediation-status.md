# TG-VLM-Curator 修复交付与验收状态

文档冻结时间：2026-11-12 11:53 UTC。仓库：`E:/Project/TG-VLM-Curator`。本轮集成代码基线为 `8fc2737`。本文逐项对应 `2026-11-12-remote-pr-and-functionality-review.md` 的 33 个 finding；历史报告保留审查当时的状态，不用于表示当前合并进度。

## 交付结论与远端门禁

本轮完成了历史审查所列实现修复，建立真实本地 PostgreSQL16、Redis/Celery、生产 factory、浏览器和 Docker 部署验证。最终本机全量为 `596 passed, 2 warnings in 281.56s`，JUnit 独立统计 `tests=596, errors=0, failures=0, skipped=0`，包含独立操作系统进程重启测试。完整部署演练退出 0。这些结果证明下述本地受控边界，不代表已经接入真实 Telegram、真实外部推理服务或完成生产上线。

| PR / 模块 | 已确认状态 | 合并或集成证据 |
| --- | --- | --- |
| #27 / M6 | 已合并 | `99696fa2` |
| #28 / M7 | 已合并 | `28c3e6fd` |
| #29 / M8 | 已合并 | `b0e955f4` |
| #30 / M9 | 已合并 | `0007be1e` |
| #31 / M10 | 交付PR，base为main；代码head `8fc2737` 已通过全部GitHub CI | PR run `43212224146` 与push run `43212215720` 的python/web/deployment全部SUCCESS；远端pytest为596 passed、1 warning、127.86秒。文档提交仍按最终head的全部checks门禁合并，merge commit以PR记录为准。 |

最终合并动作以对应 head SHA 的 GitHub checks 全部终态成功为前提，并使用 match-head-commit 校验；实际 merge commit 以远端 PR 记录为准。无冲突、已推送或单个 job 成功均不能替代这一门禁。

## 验证边界与证据索引

本地测试使用隔离 Docker PostgreSQL16、Redis7、migrations 创建的数据库、真实 SQLAlchemy 仓储与加密 vault。业务真相保存在 PostgreSQL；Redis/Celery 仅传 UUID 唤醒。Telegram 和推理服务在普通回归中使用受控外部 fixture，本地 HTTP 推理 fixture 能捕获实际请求，但不代表真实模型的输出质量或供应商兼容性。

下列证据路径均相对仓库根目录。`.git/fix-2026-11-12/` 是本机证据目录，不随普通 Git 提交分发；对外归档须先检查脱敏，仅复制报告需要的日志、结果和截图，不复制服务配置、环境文件、凭据或密钥。

| 门禁 | 实际结果与证据 | 适用边界 |
| --- | --- | --- |
| 最终 Python 全量 | `main/final-full.txt`、`main/final-full.xml`：596 passed，2 warnings，0 error/failure/skip，JUnit time 281.558 秒 | 真实本地集成与领域/fixture 用例共同组成，不能把全量数等同外部服务用例数。 |
| 原生持久化/运行集成 | 最终 JUnit：M6 6、M7 6、M8 8、media_profile 6、runtime_closed_loop 3、telegram_revocation 1、worker_process_restart 1，共31项 | 另有17项 offline_sql，明确分开，不将离线 DDL 计入原生 PG。 |
| 分层合并前全量 | `main/pr27-full.txt` 421 passed；`pr28-full.txt` 478；`pr29-full.txt` 501；`pr30-full.txt` 502 | 各自代码基线的历史门禁，与最终596存在重叠，不能相加。 |
| M8 联合与增量 | 主线联合回归12 passed；`main/pr29-cleanup-delta.txt` 1 passed、7 deselected；最终 M8 文件8项全部通过 | 联合12与M8原生8为不同集合，不混称12项原生M8。 |
| M9 浏览器 | `main/pr30-native-independent/browser-results.json`：17组全过，`errors=[]`、`complete=true`；同目录截图、数据库核查与清理结果 | 原生 PG16 + FastAPI + SQL vault + Chrome生产bundle；没有 DTO 回放或 InMemory 持久层。未启动清理worker，不含真实媒体删除、Telegram或provider调用。 |
| M9 前端 | 24项 Vitest、typecheck/build通过；`pr30/unit.log`、`typecheck.log`、`build.log`；`main/web-audit.json`漏洞总数0 | audit是本次依赖快照结果，不是所有安全风险均被排除。 |
| FIXED / LATEST runtime | `runtime/fixed.json`、`latest.json`、`final-run.txt`；最终 `test_runtime_closed_loop.py` 3项通过 | 真实PG/Redis/Celery与生产组合入口、实际归档像素和本地HTTP；外部Telegram/provider为fixture。 |
| 独立进程重启 | `runtime/restart-run.txt` 1 passed；`restart-latest.json`；`tests/integration/test_worker_process_restart.py` | 真实PG/Redis；远端fixture已产生效果、响应及PublishRecord未提交时kill，4秒租约后不同worker PID恢复；5条PublishRecord对应5个唯一外部fixture效果，保留旧记录。 |
| 完整 Docker 部署 | `main/final-deployment.txt`，完整命令退出0 | 十服务启动与重启、真实ffprobe/帧生成及运行中FFmpeg取消、TLS Secure cookie登录、全应用SIGTERM、PG16/媒体/独立key恢复、故意orphan非零和clean scan、wrong-key检查、自有两project及卷清理。使用本机临时证书，不证明生产证书信任。 |
| 静态与依赖 | Ruff全282文件、pip check、compileall通过；nullable JSON单测增量 `8fc2737` 在SQLAlchemy2.0.52/2.1.8均通过 | 代码head的GitHub python/web/deployment全部SUCCESS，文档最终head仍独立核验。 |

最终596全量包含先前失败路径的回归。中间一次590 passed / 5 failed已定位：E2E图像fixture需提供真实有效像素；另4项来自进程内Alembic `Config(alembic.ini)` 的 `fileConfig` 禁用既有worker logger。M7离线测试改用独立 `Config(output_buffer=...)` 和 `script_location`，保留诊断断言；M7、media runtime与E2E联合52项通过后再跑最终全量。两条剩余警告是Starlette/httpx和anyio弃用提示，不是失败或跳过。

GitHub首轮在新安装的SQLAlchemy2.1.8下出现595 passed/1 failed：nullable JSON单测依赖通用PostgreSQL默认driver的bind_processor。隔离安装2.1.8复现后，单测改为显式使用生产asyncpg dialect的dialect_impl，保留SQL NULL和正常JSON对象绑定断言；SQLAlchemy2.0.52与2.1.8增量均通过。提交 `8fc2737` 的远端全量恢复为596 passed，所有31项原生集成及三个CI job均通过，没有固定旧依赖或删除失败断言。

## 33项 finding 修复映射

以下“本地通过”表示相应修复及本轮本地证据成立，不自动扩展为生产/外部服务验收。代码head已获得远端CI终态；文档最终head继续受相同合并门禁约束。核心修复提交可由 `git show` 定位；不同模块分支的回移提交可能具有不同SHA，最终以合并图和当前文件为准。

### M6：发布、清理与升级（#27）

修复入口：`206f797` / `1e47bfd`，合并 `99696fa2`。原生回归入口为 `tests/integration/test_m6_postgres_regressions.py`，应用层受保护内容回退在 publication worker 回归中覆盖。

| ID | 修复与证据 | 状态及限制 |
| --- | --- | --- |
| M6-01 | 清理confirm先持久化父job再写items；PG非空确认、原子回滚和幂等用例通过。M9原生HTTP另验证1个job/60个items入库。 | 本地通过；浏览器确认没有执行删除。 |
| M6-02 | 迁移事务安全处理历史rendered字段回填和immutable触发器；带M5历史数据upgrade/downgrade、失败回填回滚及保护恢复在PG16运行。 | 本地通过，不以离线SQL代替。 |
| M6-03 | 首次发送与in-flight恢复共用protected-content fallback，metadata_only/skip/fail策略保持一致。 | 应用回归通过；真实Telegram受保护消息仍需授权合约验收。 |

### M7：流水线、去重与水位（#28）

修复入口：`e61ca0c` / `2a6fa06`，合并 `28c3e6fd`。原生回归入口 `tests/integration/test_m7_postgres_regressions.py`；专项证据包括 `m7-full-gate.txt`、`m7-final-concurrency.txt`、`m7-postgres-canonical.txt`。

| ID | 修复与证据 | 状态及限制 |
| --- | --- | --- |
| M7-01 | 使用确定约束名；PG实际M6→M7往返，检查正式历史保留和约束名。 | 本地通过。 |
| M7-02 | canonical建立严格有向顺序，排除不可靠donor；同秒同图保留有效根，原生PG回归通过。 | 本地通过。 |
| M7-03 | 窗口按媒体component相交，formal run绑定输入revision；跨窗口新增album part有wakeup且阻止水位越过。 | 本地通过。 |
| M7-04 | 锁定message父行并按formal输入revision保护业务唯一性；真实并发首次创建验证单一有效run。 | 本地通过。 |
| M7-05 | 重叠execution复用已有路由/发布结算，不要求不存在的新evaluation；原生测试检查单一intent。 | 本地通过。 |
| M7-06 | 禁用无法以当前Manifest证明安全的canonical直接复制；转入正常worker完整语义cache key校验，包含上下文和上游结果。 | 本地通过；安全回退不是实现新的donor缓存优化。 |
| M7-07 | 同一安全回退取消video candidates[0]复用，正常视觉worker根据实际帧和Manifest执行。 | 本地通过；视频封面相同不意味着采样帧语义相同。 |

### M8：真实管理API（#29）

修复入口：`d724f6a` / `790ebf4`；清理DTO补修 `2a1d2ef` / `a0493c6`；合并 `b0e955f4`。`tests/integration/test_m8_postgres_regressions.py` 最终8项通过。

| ID | 修复与证据 | 状态及限制 |
| --- | --- | --- |
| M8-01 | 六类版本构造去除不存在字段，PG逐类创建/克隆/读取/发布/归档；浏览器贯通十类配置及引用。 | 本地通过。 |
| M8-02 | 五类draft原子更新主内容和bindings/nodes/rules/actions，真实重新读取并验证非法变更回滚。 | 本地通过，不只断言HTTP200。 |
| M8-03 | retry仅重置本轮预算；PublishAttempt累计编号在intent锁下按历史max+1，保留全部历史及lease定位。 | M6/M8原生回归通过。 |
| M8-04 | failed Stage恢复完整父run及wakeup，不倒退成功阶段；真实worker仓储可再次claim。 | 本地通过。 |
| M8-05 | negative_label和pipeline_version筛选使用有效ORM/事实字段；原生SQL执行。 | 本地通过。 |
| M8-06 | 独立reanalysis仓储规划完整DAG，阶段完成时续建child；覆盖根→子与MEDIA资产目标，不改旧formal或自动发布。 | 本地通过。 |
| M8-07 | effective/manual按最新SET/CLEAR与模型事实解析；PG筛选与域语义一致，浏览器SET/CLEAR交叉验证。 | 本地通过。 |

原生浏览器额外发现清理API错误使用estimate字段及confirmed_by属性；补修后最小零资产DryRun201、非空confirm和8项M8原生回归通过。分析落库另修nullable JSON与InferenceCall依赖顺序（`f0bf147` / `ce045cf`），由真实runtime与最终全量覆盖。

### M9：Web管理端（#30）

修复入口：`b2f1aaf` / `efca8fa`；依赖修复 `2c09d31` / `0c20236`；合并 `0007be1e`。主线独立原生Chrome证据目录为 `main/pr30-native-independent/`；早期 `pr30/` 使用InMemory的16组回归仍只证明其原始边界。

| ID | 修复与证据 | 状态及限制 |
| --- | --- | --- |
| M9-01 | DryRun绑定过滤快照/请求代次，变更废弃旧响应；确认显示范围且防重复，真实延迟旧响应不能解锁。 | 原生浏览器通过；实际删除另属worker验证。 |
| M9-02 | initial_version与十类schema/引用对齐，补空实体Create First Draft；默认Routing和Rendering经真实路由执行。 | 原生浏览器通过；示例provider地址/模型不是可用服务。 |
| M9-03 | 对齐input_manifest_hash、顶层intents、PublishRecord和reroute DTO；ErrorBoundary保留shell。 | 原生PG详情无回放、无白屏；Manifest/token诊断可展开。 |
| M9-04 | published_version_id在所属versions中解析，来源绑定只展示本频道Profile，Dashboard共用逻辑。 | 原生创建/发布/绑定通过。 |
| M9-05 | MEDIA kind/id携带对应image_asset_id或video_asset_id；图片/视频SET/CLEAR及真实筛选一致。 | 原生浏览器通过。 |
| M9-06 | processed、duplicate_reclassify等枚举与后端一致，清理示例同步。 | 原生浏览器及契约回归通过。 |
| M9-07 | Messages/Audit/Review等latest-only请求代次防旧页覆盖新筛选和cursor。 | Messages/Audit动态竞态及Vitest通过；未单独动态验证所有同型页面。 |
| M9-08 | 注销失败保留准确状态与重试；401回登录并防旧会话响应影响新登录。 | 原生验证过期/正常注销；503注入来自早期本地InMemory HTTP和Vitest，原生模式未注入503。 |
| M9-09 | Review保留has_more/cursor并可前后页、过滤重置。 | 原生30条队列25+5可达。 |
| M9-10 | dirty草稿禁止发布，保存/发布/克隆/归档互斥，保存后读取确认新内容。 | Analysis与Routing原生交互通过。 |

身份启停/编辑、来源与目的身份调整、版本归档、会话过期、Test Bench/Manifest诊断同步补齐。手机仅抽查390px Audit布局，不声称全页面或跨浏览器验收。原生浏览器结束时专属DB已移除、Chrome与HTTP服务关闭；没有执行清理worker。

### M10：生产组合入口与部署（#31）

实现入口：`9fcc412`、`2189a63`、`1d2fc86`、`b33a1e3`、`93caec9`、`a7e1b17`。配置操作见 `docs/operations/runtime.md` 和 `docs/operations/deployment.md`。

| ID | 修复与证据 | 状态及限制 |
| --- | --- | --- |
| M10-01 | 默认factory解析已发布配置与加密vault，接通历史/实时Telegram、媒体、外部HTTP推理和发布；身份停用撤销客户端及已排队旧事件。FIXED/LATEST、实时重连/编辑/删除、独立进程重启通过。 | 本地真实基础设施通过；Telegram/provider依然是受控外部fixture，真实账号/模型未接入。 |
| M10-02 | nginx HTTPS与HTTP重定向、只读证书、Secure cookie及代理信任边界；真实容器TLS登录/auth-me通过。 | 本地部署通过；自签测试证书不等于生产可信证书部署。 |
| M10-03 | 统一--confirm-restore，使用Compose PG16服务与named volume；校验和、停写、独立key恢复、wrong-key和一致性错误退出演练。 | 完整本地部署通过，未操作生产库/卷。 |
| M10-04 | 新增31项原生集成、PG/Redis CI服务、十服务启动重启与恢复演练；最终本地596无skip，独立OS重启包含在内。 | 本地与GitHub代码门禁通过；合并仅针对最终head全部checks成功时执行。 |

独立重启与同进程重建是两类不同证据。独立测试在外部fixture效果已落地、响应未返回时强制结束旧worker，通过短租约及新PID恢复；wrapper PID与worker PID分别记录，不能混称同一进程。最后四种发布模式均sent，5条记录匹配5个唯一fixture效果，旧记录不变；证据记录专属DB删除、Redis键清零、子进程全部退出。主代理另实时查询确认 `remaining_runtime_test_databases=[]`、`remaining_runtime_redis_keys=[]`。Docker演练的应用SIGTERM与FFmpeg取消是独立的优雅停机证据，不能用故意kill的非零退出码代替。

### 视觉输入：已合并M4中的跨模块缺口

修复入口 `3b4f42d`；专项 `tests/unit/test_visual_input_resolution.py`，生产链路与冻结媒体配置 `9365cc8` 共同由原生runtime/media profile回归覆盖。

| ID | 修复与证据 | 状态及限制 |
| --- | --- | --- |
| VISION-01 | 异步解析真实归档图片/视频帧，校验Hash与媒体内容，发送实际视觉parts；缺失/损坏/不完整失败关闭。Manifest v2记录实际发送顺序、Hash、目标/帧映射，正式worker和Test Bench共用准备入口。 | 本地像素、HTTP捕获和PG链路通过；不代表真实模型准确率。 |
| VISION-02 | raw按实际图/帧执行max_images；contact_sheet真实拼图/区域映射，adaptive预算内raw、超预算有限拼图，容量不足明确失败且不截断丢帧。 | 预算/像素映射回归通过；不把未验证的任意供应商能力当支持。 |

冻结的Source Profile引用实际驱动image_max_dimension、webp_quality、video_max_frames。历史已发布版本继续可用；显式配置缺失、非法或重叠冲突失败关闭，瞬时DB异常可重试；不自动重写旧READY资产。旧视觉结果与缓存是否需要重分析、历史媒体是否需要重新归档，应按实际数据和用户决策执行，本轮没有批量修改生产历史。

## 最终交接与保留边界

本轮交付包含修复实现、回归测试、运行接入指南、部署恢复说明和逐项证据映射。README、模块实施文档与AGENT_HANDOFF使用相同验收边界；历史review保留原始审查基线。提交和合并不携带服务配置、真实凭据、主密钥或.git内原始测试证据。

合并必须核对最终文档head对应的全部GitHub checks，并校验提交SHA；实际合并结果以远端PR的mergeCommit为准。真实Telegram测试身份/频道、外部provider及生产TLS/备份部署仍需另行授权验收。缺少真实外部凭据不影响本轮受控门禁结论，fixture发送不得写为真实Telegram投递，受控JSON不得写为模型效果验收。测试共享容器仅按明确所有权清理，不宣称全机Docker均已停止。
