# TG-VLM-Curator 远端 PR 与整体功能审查

审查日期：2026-11-12。仓库：`Chino0w0/TG-VLM-Curator`。审查基线：`d4b969987f99fcbca43fccbf58a64274bf4b193b`，分支 `module/m10-deployment-and-operations`；`origin/main` 为 `78d3d3e2d63c312301b8fdb97fd3eab3e6d3f2d4`。

## 总体结论与范围

结论：#27、#28、#29、#30、#31 当前全部暂不合并，建议 Request changes；这是审查建议，本轮没有向远端提交 Review。各 PR 都存在自身需修复问题，并非仅因上游未合并而机械暂缓。项目已具备不少领域与模块实现，但目前不能认定为可在 Compose 中长期运行的首版完整闭环。应先修复升级/数据安全/漏处理/真实配置等阻断，再补真实持久化、队列和生产入口验收。

本轮盘点远端全部 31 个 PR，对仍需合并决策的 #27–#31 审查代码、调用契约和测试证据，并对照架构第 45、48、49 节核查整体功能。其余 24 个已合并、2 个已关闭的 PR 做历史状态核对，不冒充对全部历史代码重新逐行审查。#1/#2 已关闭，相同提交分别也用于历史 #19/#4，不建议重开。

五个待合并 PR 均为 `MERGEABLE/CLEAN`，但 `statusCheckRollup` 为空，reviews/comments 也为空。无冲突只说明 Git 能合并，不代表功能验收通过。仓库没有被跟踪的 `.github/workflows`；三次 9 月历史 Copilot Review run 不能替代本轮 CI。

| PR | 模块 / head | 当前 base | 决策与主要原因 |
| --- | --- | --- | --- |
| #27 | M6 / 2163124 | main | 暂不合并：2项P1、1项P2；历史升级与非空清理确认失败 |
| #28 | M7 / 0f82278 | module/m6-publication-lifecycle | 暂不合并：3项P1、4项P2；迁移错误、去重全跳过、媒体组漏处理 |
| #29 | M8 / 6b086aa | module/m7-pipeline-orchestration | 暂不合并：1项P1、6项P2；真实配置创建/更新、筛选、恢复和重分析缺陷 |
| #30 | M9 / eb08a7b | module/m8-admin-api | 暂不合并：3组P1、7项P2；清理安全竞态、首版配置丢失、详情白屏等 |
| #31 | M10 / d4b9699 | module/m9-web-admin | 暂不合并：生产入口未接通、HTTP/Secure Cookie不匹配、备份与真实集成门禁缺口 |

这是一条 M6→M7→M8→M9→M10 的堆叠链，只有 #27 以 main 为 base。修复后应按顺序推进，每合并一层再核对下一层的 base、差异及 CI，必要时 retarget 到 main。不要把“合入上一个模块分支”当作“进入 main”，也不要直接合并末端分支绕过审查。

本轮没有合并、关闭、提交远端 Review、推送 PR，也没有修改业务源码。交付新增审查报告，本地复现和原始证据在 `.git/review-2026-11-12/`，不会自动随 Git 提交。

## 1. PR #27：发布、清理与升级

### M6-01 · P1 · 非空归档清理确认违反外键

位置：`tgcurator/infrastructure/database/cleanup_repository.py:588–608`；外键见 M6 migration `:743–748`。

confirm 同时 add 新 job 和 items，没有先 flush 父记录，也没有 ORM relationship 表达依赖。调用真实仓储，读取使用夹具、写入使用生产 ORM mapper 和启用外键的内存 SQLite，首先执行 `INSERT INTO archive_cleanup_items`，随后报 `IntegrityError: FOREIGN KEY constraint failed`。测试只将 DDL 的 JSONB 适配为 JSON，未连接 PostgreSQL。

Dry-Run 命中至少一项资源时，确认事务无法完成，清理不能启动。应保证父 job 已插入再创建 items，并补实际 PostgreSQL 非空确认测试。证据：`pr27/review_repros.py`、`pr27/repros-head.txt`。

### M6-02 · P1 · 有历史发布意图时 M5→M6 升级被触发器阻断

位置：`migrations/versions/e6d4a8b2c951_m6_publication_cleanup_operations.py:219–224`；既有保护见 M5 migration `d7b3f9a1e6c2_m5_human_review_routing.py:883–906`。

M6 为非 forward_only 的历史 intent 回填 rendered_text/rendered_snapshot_hash。此时 M5 的 BEFORE UPDATE 触发器仍比较整行 JSONB，只排除 status/updated_at，回填会触发 `publication intent identity is immutable`。直到 M6 第 424 行才替换函数，且新函数仍不允许任意改渲染字段，单纯提前替换不够。

证据为实际 migration 顺序、确定的比较条件及 `pr27/migration_trigger_repro.sql`；未在原生 PostgreSQL 执行升级，不把静态证明称为升级实测。应在同一迁移事务内安全处理触发器与回填边界，补带旧发布数据的升级、回滚和保护恢复测试。

### M6-03 · P2 · in-flight 恢复不遵守 protected-content fallback

位置：`tgcurator/application/publishing/worker.py:60–66`，对照 `:92–112,144–150`。

首次发送遇到 TelegramProtectedContent 会按 metadata_only/skip 回退，恢复中的 reconcile-or-publish 却绕过处理，直接终态失败。调用真实 PublicationWorker、替换 Telegram/repository 边界，得到 metadata-only 首次 sent、恢复 failed；skip 首次 cancelled、恢复 failed。

应统一首次执行与恢复策略并补交叉测试。上述三项均来自 #27，涉及文件从 #27 到当前累计 HEAD 未变化。

## 2. PR #28：流水线、去重与水位

### M7-01 · P1 · Alembic 删除的约束名称不存在

位置：`migrations/versions/f1a7c3d9e204_m7_pipeline_dedup_watermarks.py:22–25`。

完整约束名未用 op.f() 标识，naming convention 再加一遍前缀。实际生成的 M6 DDL 有 `ck_range_executions_range_execution_status`，M7 却尝试 DROP `ck_range_executions_ck_range_executions_range_execution_status`，正常 M6 数据库无法继续升级。新增检查约束也出现双前缀。

已核对真实 Alembic SQL 输出 `pr28/m6-schema.sql`、`pr28/m7-upgrade.sql`，未执行原生 PostgreSQL DDL。应修复命名并在 migrations 创建的 M6 数据库执行 upgrade/downgrade，不能只断言生成文本包含某个子串。

### M7-02 · P1 · 同秒同图消息互为 canonical，全部跳过

位置：`tgcurator/infrastructure/database/pipeline_repository.py:905–926`、`tgcurator/application/pipeline/coordinator.py:340–353`。

候选允许 published_at 相等的 unchecked 消息；精确指纹分支又不排除 duplicate_skipped。两条同秒同图消息串行执行即可得到：100 指向 101 并跳过，101 又指向 100 并跳过，随后 watermark=101、execution=completed。有效内容全部漏分析/路由，窗口却完成。

复现执行生产候选查询表达式和真实 coordinator，查询仅投影 ID、在 SQLite 运行，不依赖并发。应建立严格有向 canonical 顺序，排除未确定/跳过 donor，保证每个等价组至少一个有效根。

### M7-03 · P1 · 媒体组跨窗口时新增 part 被漏处理

位置：同仓储 `:265–269,156–159`，coordinator `:170–186`。

窗口只看逻辑消息 primary ID。媒体组 primary=100，后来合入 part101；下一窗口 (100,101] 不包含 parent100。实际查询得到 messages=0，coordinator 完成 execution、水位到101，而新 asset 仍 pending 且无 wakeup。

已运行真实窗口筛选表达式与 coordinator，未运行完整 PostgreSQL history-ingestion 事务链。应按 component 与窗口相交及媒体组版本变化维护归属/待处理状态，保证水位不能越过未处理 part。

### M7-04 · P2 · 首次 formal AnalysisRun 创建不具并发幂等性

位置：同仓储 `:425–446`。

先查 run 并 FOR UPDATE，查不到就插入；但未锁定始终存在的 message 父行，也没有 formal 业务唯一约束。运行真实方法的受控异步交错，两个调用各建一个 run，实际 DDL 无对应 unique constraint/index。可能重复推理、写标签，并掩盖另一套运行状态。

这是受控交错与 DDL 核查，不是 PostgreSQL 双连接并发实测。应增加正确锁/业务唯一性，并补真实双连接首次创建测试。

### M7-05 · P2 · 重叠范围卡在缺少新 execution 的 routing evaluation

位置：coordinator `:209–213,621–632`，仓储 `:322–330`。

已 processed 的消息在新重叠范围被提前返回；水位结算却要求新 execution 专属 routing evaluation，流程不会补建。真实 coordinator 配合符合生产 execution 隔离语义的 fixture，连续三次推进仍为 awaiting_pipeline、watermark=99、routing_calls=0。应统一跨范围复用和本执行结算语义，不重复发布也不永久阻塞。

### M7-06 · P2 · canonical 缓存绕过语义输入校验

位置：仓储 `:1433–1447,635–706`。

只排除 text=True，未核对 previous_results/source context 等输入，也不重建当前 Manifest/比较 semantic cache key，就复制 donor 的结果与标签。合法的 MESSAGE_VISUAL Stage 在同图、不同上游结果时，正常 orchestrator 的 cache key 不同，M7 helper 却仍返回旧 donor，可能错误激活负向门禁。应复用统一语义缓存规则，而非仅比较 stage 版本。

### M7-07 · P2 · 多视频组重复使用第一个 canonical 视频结果

位置：仓储 `:1452–1487`。

多 asset 匹配仅实现 image_asset，video_asset 直接返回 candidates[0]。真实 helper fixture 中两个不同当前视频均复用同一 canonical target，导致错标签或错门禁。应按视频稳定视觉对应关系匹配，无法确定则重新分析。

上述七项来自 #28，后续未修。还有 parameter_overrides、media_processing_profile_version_id 缺少完整运行消费的配置缺口，需补契约测试；未独立完成复现的水位提交竞态不另算确定缺陷。

## 3. PR #29：真实管理 API 与 InMemory 不等价

### M8-01 · P1 · 六类配置版本无法创建

位置：`tgcurator/infrastructure/database/admin_api_repository.py:2593–2712`。

_insert_db_version 为 label-definitions、prompt-templates、inference-profiles、label-sets、analysis-stages、analysis-pipelines 的版本 ORM 构造器传入 updated_at；这六类模型仅有 CreatedAtMixin，没有该字段。逐类调用真实方法，都在 SQL 发出前抛 `TypeError: 'updated_at' is an invalid keyword argument`。同样 label draft 在 InMemory 成功。

这直接阻断核心初始配置。应对齐 mapper/schema 与 API 字段，逐类验证新建、克隆、读取、发布和归档。

### M8-02 · P2 · 五类 draft 保存成功但内容没更新

位置：同文件 `:2798–2827`，入口 `:794–824`。

_apply_db_version_update 未实现 inference-profiles、label-sets、analysis-stages、analysis-pipelines、routing-policies 的内容/子记录更新。实际五类对象均为 requested=new、actual=old、flushes=1，上层却正常返回并写 version_updated 审计。应原子更新主内容及 bindings/nodes/rules/actions，重新读取验证，不只检查 HTTP 200。

### M8-03 · P2 · 发布 retry 重用历史 attempt 唯一键

位置：同文件 `:3065–3077`，对照 `publication_repository.py:195–206` 和 uq_publish_attempt_number。

retry 清零 attempt_count，却保留历史 PublishAttempt，下次 claim 又插入 attempt_number=1。运行真实 reset/claim 捕获的新记录，与历史 (intent_id,1) 相同，确定冲突于 PostgreSQL 唯一约束；未在原生数据库实际 INSERT。应分开累计审计编号与重试预算，编号保持单调，不删历史。

### M8-04 · P2 · 分析 retry 不恢复 failed 父 run

位置：同文件 `:3053–3064`，对照 `analysis_repository.py:203–207`。

StageRun 改 pending，父 AnalysisRun 仍 failed，worker 拒绝领取。复现为 stage=pending、parent=failed、claim=None。应按规则恢复完整因果状态与 wakeup，验证能被 worker 领取并继续结算，而非仅返回 pending。

### M8-05 · P2 · 两项消息筛选引用不存在的 ORM 属性

位置：同文件 `:1059–1069`。

negative_label 引用 MessageRecord.blocked_by_label_key；pipeline_version_id 引用 AnalysisRunRecord.pipeline_version_id，实际字段是 analysis_pipeline_version_id。真实 list_messages 分别抛 AttributeError，SQL 发送量为零；前端筛选控件会暴露这些路径。应修正字段，对全部筛选及其组合执行真实仓储查询。

### M8-06 · P2 · 重分析只执行根节点便显示 completed

位置：同文件 `:1605–1629`，对照 `analysis_repository.py:880–921`、`pipeline_repository.py:311–316`、`tgcurator/application/pipeline/coordinator.py:113–132`。

reanalysis 只创建无依赖节点，没有为该 run 接后续 DAG 调度；现有 coordinator 又只加载 formal run、经 awaiting range 推进。对 root→child 两节点 pipeline，调用真实 reanalyze/完成方法得到 StageRun 数=1，根结束后 run=completed、child 未创建。应为重分析实现完整且独立的 DAG 进展，不覆盖旧 formal 历史，也不自动重发。

### M8-07 · P2 · effective/manual 筛选按历史存在性而非当前事实

位置：同文件 `:1115–1146`。

SQL effective 把历史 activated model 与历史 activated manual SET 做 OR，没有最新事件覆盖及 CLEAR 语义。模型 true、后续人工 false 时，domain resolver=False，仓储实际生成的 EXISTS SELECT 仍命中。复现执行生产查询生成与 SQLite 关系谓词，不是 PostgreSQL 集成测试。应按消息/目标/作用域/标签与 (created_at,event_id) 解析最新事实，和详情、Routing 统一语义。

七项均属 #29，相关实现到当前 HEAD 未改。主代理已重跑四个专项 probe 并复核代码。产物在 pr29 目录：admin_contract_probe、retry_filter_probe、reanalysis_probe、effective_filter_probe 的 .py/.out。

本轮未确认鉴权绕过或 Secret 明文泄漏；所查写接口除 login 外要求 CSRF，已有 session/CSRF/write-only-secret 测试通过。这不等于原生数据库安全验收。

## 4. PR #30：Web 管理端

### M9-01 · P1 · 旧 Dry-Run 响应重新解除清理安全锁

位置：`web/src/workspaces/ArchiveManagementWorkspace.tsx:100–102,126–154`。

过滤变化只清空当前 preview，不取消在途请求、也不检查请求代次。真实 Chrome 中将 archived_before 从 2026-12-01 改为 2026-10-01，旧响应到达后又激活旧 preview，确认提交的旧 ID/hash 恰好相互匹配，API 接受包含30个11月资产的任务。当前10月条件本不应命中这些资产。截图 `05-cleanup-stale-preview.png` 与 browser-results2.json 记录了表单/确认错配。本轮只创建内存任务，未执行任何删除。

这独立于 M6 真实清理确认的外键缺陷；不能依赖另一个 bug 暂时阻止删除来放行。应将请求绑定过滤快照与代次，条件变化废弃旧响应，确认中显示可读范围而非只有 hash，并限制重复确认。

### M9-02 · P1 · 新建配置没有首版，控制台无法恢复

位置：`web/src/workspaces/AnalysisConfigWorkspace.tsx:230–248`、`RoutingWorkspace.tsx:228–238`；后端数据库契约 `admin_api_repository.py:738–745`。

前端发 {name,content}，实际接口读 initial_version。真实本地 HTTP 返回201、versions=[]，界面却提示创建了 DRAFT v1；编辑/发布/克隆控件只在已有版本时出现，因此无法从 UI 补首版。Rendering Template 和 Routing Policy 同样失败。

仅修请求键还不够：默认 Label JSON 缺 display_name；LabelSet 应用 bindings 而非 label_definition_version_ids；Prompt 的 text 变量不在声明契约；Inference 应用 api_secret_reference/structured_output_support，而非 api_key_secret_id/supports_structured_outputs；Rendering 应用 template_text；Routing 默认 DSL label_activated 及模式 copy_publish 不合法；媒体配置字段也与实际 image_max_dimension/video_max_frames 不一致。已把原样 JSON 提交到正确 versions API，得到实际422或未按预期解释的 JSON。应统一 schema、提供正确引用选择并验证空安装完整配置流程。

### M9-03 · P1 · 已分析/路由消息详情使整个控制台白屏

位置：`web/src/workspaces/MessageDetailWorkspace.tsx:324,414–415`，错误类型定义在 `web/src/api/types.ts`。

真实接口返回顶层 publication_intents，前端却读取 ev.publication_intents.map；真实 Manifest 序列化返回 input_manifest_hash，前端读 manifest_hash.slice。前者用本地真实 reroute/详情 HTTP 响应，后者用真实数据库 serializer 输出回放，均在 Chrome 产生 undefined.map/undefined.slice，body 为空、导航也消失。主代理复核代码和白屏截图。

应对齐 DTO，按 evaluation ID 关联顶层 intents，读取正确 Hash，并增加真实 HTTP/serializer 驱动测试。ErrorBoundary 可保留 shell，但不能代替契约修复。

### M9-04 · P2 · 已发布来源 Profile 无法绑定

位置：`web/src/workspaces/SourceChannelsWorkspace.tsx:99–103`。

前端读 published_version，真实实体返回 published_version_id+versions。用本地真实 API 创建并发布 Profile 后，下拉仍只有 Unassigned。Dashboard 同型字段错误还可能误计已发布数量。应按 ID 解析具体版本，验证发布→源频道绑定。

### M9-05 · P2 · MEDIA 人工标签缺少资产身份

位置：`web/src/workspaces/ReviewWorkspace.tsx:129–138`、`web/src/api/client.ts:439–451`。

发送 media target_id，却漏 image_asset_id/video_asset_id，且视频缺正常选择路径。Chrome 实测提交图片标签返回422 `target identity is inconsistent`。应使用 kind/id 资产选择器，确保 set/clear 均带一致身份，覆盖图片与视频。

### M9-06 · P2 · 成功状态筛选值与后端枚举不一致

位置：`web/src/workspaces/MessagesWorkspace.tsx:218–222,253–257`。

UI 用 completed，实际消息状态是 processed；用 duplicate_reclassified，实际是 duplicate_reclassify。浏览器30条 processed 消息选 completed 后结果0，直接同API查 processed 有30条。应统一枚举来源，清理页示例同改。

### M9-07 · P2 · 旧分页响应覆盖新筛选结果

位置：同文件 `:69–84`。

loadPage 没有取消/请求代次检查。Chrome 延迟第二页响应后提交新查询，先出现正确1条，释放旧响应又变5条，而表单仍是新条件；游标也被覆盖。应只允许当前查询/游标响应写状态，合理禁用加载中翻页。其他同型页面需一并测试，不冒称全部已动态验证。

### M9-08 · P2 · 注销失败显示假成功

位置：`web/src/App.tsx:90–95`。

finally 无条件清 session，服务器 logout=503 时显示登录页但实际会话未失效，刷新再次登录。Chrome 已验证该失败路径及正常 logout 后401对照。应准确提示服务端注销失败、保留重试，不声称会话已撤销。

### M9-09 · P2 · Review 队列截断于25条

位置：`web/src/workspaces/ReviewWorkspace.tsx:60–70,259–289`。

只保存 page.items，丢弃 has_more/next_cursor，没有翻页入口。API 有30条未审核且 has_more=true，队列只25条。应增加游标分页或加载更多，切换过滤重置，不能以从 Messages 绕路当作队列已完整。

### M9-10 · P2 · 未保存的草稿可直接发布旧内容

位置：`web/src/workspaces/AnalysisConfigWorkspace.tsx:285–305,463–469`；Routing 同型 `:271–288,521–528`。

Publish 不检查 dirty editor，也不保存编辑内容；成功 reload 会覆盖用户输入且已发布版本变 immutable。Chrome 将 old description 改为未保存新内容后直接发布，返回200但仍是old。应禁用 dirty 发布或明确 Save and Publish 串行操作。

本轮运行本机真实 Chrome headless，加载生产 Vite bundle；本地服务只监听127.0.0.1，使用真实 M8 HTTP路由/鉴权/服务与 InMemory 后端。Manifest 场景为实际 serializer 回放，不是数据库 E2E。桌面和手机抽查有截图，主代理复核清理/白屏/标签截图、JSON证据及关键代码。原始测试脚本的一次超时后已完成专项补测，不能把初次未跑到的部分当通过。

完整前端细证据和10项发现为 `pr30/report.md`、browser-results*.json、manifest-browser-result.json 及截图。后续产品补项还包括身份启停/编辑、来源/目的地读写身份调整、路由版本归档入口、会话过期回登录，以及完整 Manifest/Test Bench 错误与token诊断视图。protected_content 控件未被对应创建映射持久化，也需统一契约。以上无需扩成复杂权限系统。

## 5. PR #31：部署声明尚未形成可运行产品

### M10-01 · P1 · 默认生产入口没有接通核心适配器

位置：`apps/worker/runtime.py:213–248`、`apps/telegram_ingestor/__main__.py:28–53,75–85`、`apps/telegram_ingestor/runtime.py:178–220`。

构造真实 factory：range history handler、image/video worker、publication worker 均为 None，analysis provider=None；ingestor reconciliation=None、connected=False。daemon 只 flush 缓冲区，没有 Telegram 连接、事件订阅或启动补抓。一次真实 daemon spy 执行为 flush=1、shutdown=1、connect=0、reconcile=0。

适配器的类/端口存在，不等于界面填入身份和 Profile 后能在运行入口使用。缺配置时拒绝伪造成功是正确边界；但始终没有配置解析和注入路径，不能据此宣称架构第 49 节首版闭环完成。部分缺省是前序模块的刻意状态，#31 的问题是把未接线状态当作完整生产交付。

应读取已发布版本、通过 vault 获取 secret、管理身份/session 生命周期、选择外部 Provider，并注入历史摄入、媒体下载/存储/处理和发布器。不需要、也不应在 Compose 内部署模型。证据：deployment-probes.json。

### M10-02 · P1 · 默认 HTTP 部署与生产 Secure Cookie 不兼容

位置：`docker-compose.yml:77–86`、`docker/nginx.conf:1–3`，对照 `apps/api/routers.py:144–151`。

默认 production，Web 只暴露 HTTP:80→8080，无 TLS 终止配置，登录 cookie 带 Secure。非 localhost HTTP 站点最小复现为 login=200、auth/me=401，同应用 HTTPS 则均200。此验证使用 TestClient cookie jar，不冒充真实浏览器 TLS 验收；localhost 例外不能替代远端部署。

应提供 HTTPS/TLS 终止、可信代理约束和端到端登录验收。不要关闭生产 Secure Cookie。若必须接外部 TLS 网关，应作为部署必需条件提供运行配置。

### M10-03 · P2 · 备份恢复入口与部署环境不匹配

位置：`README.md:264–269`、`scripts/restore-postgres.sh:12–36`、`scripts/backup-postgres.sh:10–14`、`scripts/backup-media.sh:9–14`、`docker/backend.Dockerfile:1–12`、`docker-compose.yml:29–38`。

README 用 --yes-restore，脚本只认 --confirm-restore，按文档调用实际退出码2，尚未做数据库操作。宿主脚本又默认 localhost:5432 和宿主 /var/lib/tgcurator/archive，但 Compose 没暴露 DB 端口，媒体在 named volume，缺完整 docker compose exec/helper 路径。

若在 backend image 内运行备份，bookworm 的 postgresql-client 元包依赖 postgresql-client-15，而 server 是 PostgreSQL16，pg_dump15 不支持较新主版本 server。已核对 Debian 官方 deb control 和 PostgreSQL15 官方手册；这是静态镜像配置推导，未实际构建容器。证据存为 debian-postgresql-client-control.txt、pg-dump-15-manual.html。

应统一参数、实际容器/volume 访问和客户端版本，完整演练数据库+媒体+独立密钥恢复，验证校验和、失败退出、维护窗口及一致性报告。

### M10-04 · 合并门禁缺口 · E2E 未覆盖真实服务边界

位置：`tests/integration/test_alembic_offline_sql.py`、`tests/e2e/test_closed_loop_pipeline.py`；架构 `:1263–1275`。

17 项 integration 仅渲染离线 SQL，4 项 e2e 使用 InMemory 仓储及 Fake DB/Broker/Telegram/Inference。它们对领域和模拟故障有价值，却不会执行 PostgreSQL 触发器/外键/唯一键/并发租约，或验证真实 broker、Compose 重启、生产 factory。

架构要求真实 PostgreSQL+Redis，Fake 的是 Telegram/推理，而不是把持久化和队列都替换。应增加 migrations 建库的隔离 PostgreSQL16+Redis，测试有历史数据升级、CRUD、清理、并发 claim/幂等、真实 worker 和 Redis 丢失恢复。外部合约测试用单独授权凭据，普通 CI 不应要求生产 Secret。

## 6. 已合并代码中的整体功能缺口

### VISION-01 · P1 · 实际归档媒体没传入 VLM，仍接受标签

位置：`tgcurator/infrastructure/database/analysis_repository.py:1461–1506`、`tgcurator/application/analysis/input_composer.py:144–148,182–187`、`tgcurator/infrastructure/inference/openai_compatible.py:201–231`。

仓储只有 storage_backend/storage_key，Composer 复制元数据，Adapter 仅认顶层 provider_url/data_url，既不读归档也不遍历视频 frames；没有 URL 就发纯文本。媒体必填仅检查容器非空。

使用真实本地图片 fixture、仓储形状数据和 MockTransport 捕获请求，图片、视频、嵌套帧 URL、无帧视频四类输入均 image_parts=0；返回合法模拟 JSON 后 keep 标签仍通过校验。主代理已重跑和复核，未测试外部模型准确率或实际写生产标签。

它来自已合并 M4（9bc76d4），不是新 PR 回归，但独立阻断视觉产品闭环。应读取图片与选定帧、构造实际视觉输入，并对 media=True 缺视觉内容失败关闭；Manifest 记录真正发送内容/顺序/Hash/asset-frame映射。修复时评估旧视觉结果和缓存是否需失效重跑。

### VISION-02 · P2/能力缺口 · 视觉策略与图片预算没有落地

位置：`tgcurator/application/analysis/input_composer.py:65–148`、`tgcurator/infrastructure/inference/openai_compatible.py:201–231`、`tgcurator/infrastructure/database/analysis_repository.py:1152,1164`。

绕过传图断点，直接给两项 data_url，max_images=1 仍发送2张；raw/contact_sheet/adaptive 都产生相同 raw parts，无拼图或区域映射。应先完成可靠 raw 路径与实际图/帧预算，对未实现策略明确拒绝，再做拼图、自适应、拆批及映射。架构将视觉组合策略列为建议支持，应区分未执行预算的缺陷与建议能力尚未实现。证据：vision/reproduce.log、policy_*.json。

## 7. 从整体功能看，建议如何完善

优先把已有模块接成可信的真实链路，而不是继续新增页面或扩大基础设施。领域层、不可变版本、人工覆盖、确定性路由、持久化唤醒与恢复设计值得保留；主要风险集中在具体适配器接缝、真实数据库行为，以及缺少覆盖这些接缝的验证。

| 首版能力 | 当前可确认状态 | 完成所需证据 |
| --- | --- | --- |
| 从历史 M5 数据升级到当前 schema | M6 回填与 M7 约束命名有阻断 | 带历史数据的 PostgreSQL16 upgrade/downgrade/回滚 |
| 身份、来源、目的地、范围接入 | 管理入口与端口存在，生产 Telegram 接线缺失 | 测试身份完成历史+实时+断线补抓，权限真实校验 |
| 版本化分析与路由配置 | 真实 ORM 新建/保存分支有确定缺陷 | 十类配置通过实际 API+PostgreSQL CRUD/发布/锁定 |
| PreScreen 与 Negative Gate | 领域/模拟场景覆盖，默认链路未接通 | 真实 worker 证明被阻断消息不下载重型媒体 |
| 媒体标准化与视觉去重 | 原子存储/处理实现存在，跨窗口及 canonical 有缺陷 | 媒体组边界、同秒同图、多视频与重启回归 |
| 外部 VLM 多模态分析 | 实际归档输入会退化文本，预算未执行 | 捕获真实形状图/帧请求及有效 Manifest，受控 Provider 验收 |
| 人工覆盖、筛选、重分析 | 分层领域语义存在，真实筛选/重分析流程不完整 | SET/CLEAR/历史覆盖一致，完整多节点 reanalysis |
| 路由与四种发布 | 应用实现与模拟恢复存在，默认发布器未注入 | 隔离频道四模式、部分恢复、受保护回退与重复消费 |
| 连续水位与跨范围运行 | 发现可漏处理越界和永久阻塞场景 | 实际持久化跨窗口/重叠范围/失败恢复验证 |
| 清理与恢复 | 非空确认失败；备份入口与版本不匹配 | 非空真实清理、活跃引用保护、完整恢复演练 |
| Web 管理操作 | 真实浏览器确认清理安全竞态、首版创建/详情/绑定等契约失效 | 浏览器+真实 API/DB 主流程、错误与取消回归 |
| 长期运行与可恢复性 | 健康探针/守护进程骨架存在，未运行十服务栈 | Compose 重启、Redis 清空、双worker竞争、备份恢复 |

### 第一优先级：修复上线阻断，建立真实持久化门禁

按依赖顺序修复 PR 自身缺陷，尤其历史升级、清理事务、配置 CRUD、retry/reanalysis 状态、漏处理水位；同时完成实际视觉输入和生产 runtime 的配置解析与注入。

验收应从隔离环境里的管理员操作开始，贯通“身份/来源→版本配置→固定历史或 LATEST 范围→预筛→媒体→VLM→复核→路由→发布记录→重启恢复”，不能只手工调用组装好的应用对象。真实发送只能用明确授权、标识清楚的测试身份/频道，不能向生产目的地试发。

在 CI 提供由 migrations 建立的 PostgreSQL16+Redis 服务，不使用 production Secret。加入六个关键边界：历史版本升级、真实 CRUD 与外键、首次 formal run 并发、重复消费、进程中断后恢复、broker 清空重建。现有领域测试保留，但不能替代这些集成测试。

### 第二优先级：让单管理员真正自助接入和排障

组织明确的准备检查流程：Telegram session/身份导入或授权接入、来源读取/目的地发送权限验证、外部 Provider 连通性与结构化输出试跑、媒体目录读写检查。能力解析器可复用，但模拟“可发送”不能当成实际权限验证；Test Bench 必须真正使用选中的 Profile 和 vault 配置。

分开显示基础设施和业务能力状态：PostgreSQL/Redis 是否可用、Telegram 未配置/已连接/过期、Provider 未配置/不可达、实际配置版本、最后成功处理、失败原因及阻塞水位。保留“外部推理故障不拖垮 Admin API”的边界，但不要把 API ready 展示成流水线全部可工作。当前 ingestor 未接线时的 health=ok 也应有明确的能力诊断解释。

提供可定位的失败队列，清楚区分 retry、reanalysis、reroute 的影响。retry 不应重用审计编号或清历史；reanalysis 应执行完整 DAG 并保持旧结果可追溯；正式 reroute 前应展示新增目的地和可能新增发布，不自动删除旧发布记录。

### 第三优先级：补长期使用体验、容量控制与运维演练

补媒体/视频代表帧预览、实际发送 Manifest 查看、人工覆盖前后事实对照、组合筛选和浏览器主流程回归。提供经过验证的初始配置模板、引用依赖选择、克隆及配置导入导出，减少手写 JSON/UUID 的错误；配置发布前展示版本依赖和路由命中预览。

把 max_images、实际视频帧数、Provider 速率、并发、超时、磁盘和队列积压组成执行预算与背压，不能让配置字段只存在于表单。根据水位滞后、FloodWait、身份过期、空间不足等提供通知及恢复指引，以实际指标决定容量，不急于引入架构首版明确排除的 Kubernetes、复杂 RBAC 或分布式基础设施。

备份交付以“恢复后能解密身份、读取媒体、继续未完成任务”为标准，明确备份路径、校验和、保留周期、独立主密钥、轮换/回退和恢复演练。固定可复现的依赖/镜像版本；登录防滥用、TLS 与可信代理边界另做部署加固验收。未确认某类安全漏洞，不表示该风险已被全面排除。

## 8. 验证结果、复现与限制

| 检查 | 本轮实际结果 |
| --- | --- |
| 主线 pytest 全量 | 431 passed，29.60s；410 unit +17 integration +4 e2e；2 条依赖弃用警告 |
| Ruff format/check | 248 files already formatted；All checks passed |
| pip check | No broken requirements found |
| compileall | apps/tgcurator/migrations/tests 通过 |
| Alembic heads | a8b2d4e1f305 单一 head；不等于迁移实际可执行 |
| Compose config | 使用 review-placeholder 密码仅解析，--quiet 通过；未创建真实 .env/secret |
| shell 语法 | 三个 backup/restore 脚本 bash -n 通过；README 恢复命令实测 exit=2 |
| PR27 原有专项 | 86 passed；另有清理外键/受保护恢复/迁移静态复现 |
| PR28 原有专项 | 40 passed；另有实际 SQL 生成及去重/窗口/并发交错/缓存复现 |
| PR29 原有专项 | 42 passed；另有真实 ORM/应用方法及关系谓词复现，主代理已重跑 |
| 视觉原有专项 | 40 passed；主代理重跑真实归档形状→HTTP 捕获的最小复现 |
| Web | typecheck/build通过（M9隔离快照，与当前web源码一致）；主代理在实际web目录原样npm test为7/7通过；Chrome实际渲染/交互/截图确认缺陷 |
| git diff --check | 通过；业务源码/refs/lockfile未变，最后仅新增本审查报告 |

前端代理的M9快照位于.git，原样Vitest曾受Vite文件访问限制而未运行用例；仅在该隔离快照覆盖server.fs.deny后7/7通过。主代理随后在真实web目录原样npm test再次7/7通过，故不将隔离路径问题报为产品测试失败。

专项与全量测试存在重叠，不把通过数量相加。431 项通过证明的是测试实际覆盖的行为；现有 integration/e2e 没有运行真实 PostgreSQL/Redis，因此不能证明真实数据库事务、迁移、队列与部署闭环。

原始远端清单及 PR head/base、变更文件、review/comment、actions 状态存于 `.git/review-2026-11-12/pr-inventory.json`、`pr-27.json` 至 `pr-31.json` 和 `actions-runs.json`。主线结果在 pytest.xml/pytest.log，部署复现在 probe_deployment.py/deployment-probes.json，专项在 pr27/pr28/pr29/pr30/vision 对应目录。

可只读重跑的例子如下。除所列 probe 产物外，不改业务数据；这些脚本是审查辅助，不是生产操作脚本。

```powershell
& 'D:/Program Files/Git/bin/bash.exe' -lc 'PYTHONPATH=. .venv/Scripts/python.exe .git/review-2026-11-12/probe_deployment.py'
& 'D:/Program Files/Git/bin/bash.exe' -lc '.venv/Scripts/python.exe .git/review-2026-11-12/pr27/review_repros.py .'
& 'D:/Program Files/Git/bin/bash.exe' -lc 'PYTHONPATH=. .venv/Scripts/python.exe .git/review-2026-11-12/pr29/admin_contract_probe.py'
& 'D:/Program Files/Git/bin/bash.exe' -lc 'PYTHONPATH=. .venv/Scripts/python.exe .git/review-2026-11-12/vision/reproduce.py'
```

本机 Docker CLI 存在，但 Linux engine named pipe 不存在。本轮未启动 Docker Desktop、未构建/运行十服务栈，未连接原生 PostgreSQL、Redis、真实 Telegram 或推理服务。部分证据使用真实 ORM/应用方法、SQLite 外键或关系谓词，PostgreSQL 专属迁移结论明确采用实际生成 DDL/触发器静态分析。本报告不将这些称为生产验收，也不声称已经修复。

## 附录：全部远端 PR

| PR | 状态 | 标题 | 基分支 → 头分支 |
| --- | --- | --- | --- |
| #1 | 已关闭 | feat(m3): add deterministic video frame sampling | `main` → `phase/m4-video-sampling-foundation` |
| #2 | 已关闭 | feat(m2): add durable range execution foundation | `main` → `phase/m2-durable-range-execution` |
| #3 | 已合并 | Project bootstrap: establish the Python package foundation | `main` → `chore/project-package-bootstrap` |
| #4 | 已合并 | M2: add durable range execution foundation | `chore/project-package-bootstrap` → `module/m2-durable-range-execution` |
| #5 | 已合并 | M3: add idempotent Telegram message ingestion | `module/m2-durable-range-execution` → `module/m3-telegram-message-ingestion` |
| #6 | 已合并 | M3: add local atomic archive storage | `module/m3-telegram-message-ingestion` → `module/m3-local-archive-storage` |
| #7 | 已合并 | M3: add WebP image normalization and perceptual hashing | `module/m3-local-archive-storage` → `module/m3-image-normalization` |
| #8 | 已合并 | M3: connect range execution to historical message ingestion | `module/m3-image-normalization` → `module/m3-range-history-ingestion` |
| #9 | 已合并 | M3: add real-time Telegram media-group buffering | `module/m3-range-history-ingestion` → `module/m3-realtime-media-groups` |
| #10 | 已合并 | M3: add bounded Telethon history reader | `module/m3-realtime-media-groups` → `module/m3-telethon-history-reader` |
| #11 | 已合并 | M3: persist source-channel reconciliation cursors | `module/m3-telethon-history-reader` → `module/m3-source-reconciliation-cursors` |
| #12 | 已合并 | M3: preserve source-message edit and deletion lifecycle | `module/m3-source-reconciliation-cursors` → `module/m3-source-message-lifecycle` |
| #13 | 已合并 | M3: persist image archive readiness state | `module/m3-source-message-lifecycle` → `module/m3-image-archive-readiness` |
| #14 | 已合并 | M3: add compliant Telethon protected-media downloader | `module/m3-image-archive-readiness` → `module/m3-protected-media-downloader` |
| #15 | 已合并 | M3: persist image source-message references | `module/m3-protected-media-downloader` → `module/m3-image-source-references` |
| #16 | 已合并 | M3: add image archive worker workflow | `module/m3-image-source-references` → `module/m3-image-archive-worker` |
| #17 | 已合并 | M3: add persistent image archive worker leases | `module/m3-image-archive-worker` → `module/m3-image-archive-leases` |
| #18 | 已合并 | M3: add durable image archive wakeups | `module/m3-image-archive-leases` → `module/m3-image-archive-wakeups` |
| #19 | 已合并 | M3: add deterministic video frame sampling strategy | `module/m3-image-archive-wakeups` → `module/m3-video-frame-sampling` |
| #20 | 已合并 | Build: declare pytest as a development dependency | `main` → `chore/test-tooling-baseline` |
| #21 | 已合并 | M0: establish the domain foundation | `main` → `module/m0-domain-foundation` |
| #22 | 已合并 | M1: add API security and PostgreSQL foundation | `main` → `module/m1-api-security-schema` |
| #23 | 已合并 | M2: add durable processing scheduling | `main` → `module/m2-durable-processing-scheduling` |
| #24 | 已合并 | feat(m3): add Telegram ingestion and archive workflows | `main` → `module/m3-telegram-ingestion-archive` |
| #25 | 已合并 | feat(m4): add versioned analysis engine and provider adapters | `main` → `module/m4-analysis-engine` |
| #26 | 已合并 | feat(m5): add human review and routing workflows | `main` → `module/m5-human-review-routing` |
| #27 | 待合并 | feat(m6): add publication lifecycle, archive cleanup, and operations | `main` → `module/m6-publication-lifecycle` |
| #28 | 待合并 | feat(m7): add end-to-end pipeline orchestration, visual deduplication, and continuous watermarks | `module/m6-publication-lifecycle` → `module/m7-pipeline-orchestration` |
| #29 | 待合并 | feat(m8): add admin HTTP API, session security, prompt test bench, and metrics | `module/m7-pipeline-orchestration` → `module/m8-admin-api` |
| #30 | 待合并 | feat(m9): add React TypeScript web administration console | `module/m8-admin-api` → `module/m9-web-admin` |
| #31 | 待合并 | feat(m10): add Docker Compose topology, daemon lifecycles, backup consistency tools, and E2E suite | `module/m9-web-admin` → `module/m10-deployment-and-operations` |

## 交付状态

远端在 `2026-11-12T09:52:25.790120+00:00` 再次核对：待合并集合仍为 #27–#31，head/base 与审查快照完全一致，五个 PR 仍 OPEN，checks 均为空。五个专项代理已收齐结果并关闭；本轮测试服务、浏览器 context 和进程已结束，18730 无监听、审查路径进程为零。只新增本报告，未改业务代码、未提交或推送。审查与合并决策已交付；缺陷修复、实际合并及生产验收不在本轮实际操作中。
