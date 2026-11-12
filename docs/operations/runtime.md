# 运行接入与真实验收边界

本指南适用于修复后的默认生产入口。PostgreSQL 是业务状态源，Redis 是可重建唤醒；外部推理服务不由本项目部署。先按 deployment.md 配置可信 TLS、独立保存主密钥、迁移数据库并初始化管理员。

## Telegram 身份

守护进程不会在后台交互式询问手机号、验证码或二步验证密码。MTProto 用户身份必须先由有权操作该账号的人员获得已授权 Telethon StringSession，再把以下 JSON 整体存入界面的 write-only Secret，并将 Telegram Identity 引用到该 Secret：

```json
{
  "api_id": 123456,
  "api_hash": "<telegram application api hash>",
  "session_string": "<authorized Telethon StringSession>"
}
```

机器人身份的 Secret 格式如下。api_id/api_hash 是 Telethon MTProto 应用凭据，bot_token 是机器人凭据；它们不能互相代替。

```json
{
  "api_id": 123456,
  "api_hash": "<telegram application api hash>",
  "bot_token": "<test or production bot token>"
}
```

这些示例没有可用凭据，不要原样使用。不要把真实值贴到聊天、Git、日志、截图或普通频道配置。JSON 经已有 Secret API 加密存入 vault，界面不会提供明文重新读取。程序也支持从进程设置取得 telegram_api_id/telegram_api_hash 的后备值，但推荐将完整凭据组合放在加密 Secret 内，避免普通 .env 明文。原始 session/token 仅在同时显式提供应用凭据时可用。

来源频道的 default_read_identity_id 决定历史读取、下载和实时订阅身份；目的频道的 default_publish_identity_id 决定发布身份。确保来源读取和目的发送权限真实存在。历史扫描通常应使用 MTProto 用户；机器人历史能力受 Telegram 限制，能力模拟器不代表已经获得远端授权。

默认进程在外部调用前读取加密身份，客户端只在当前进程/事件循环内使用。Secret 旋转或断连会替换旧客户端；实时 ingestor 按身份注册新消息、编辑与删除事件，并在启动、重连和定期轮询时进行幂等历史补抓。身份连接失败只记录错误类别，不回显凭据。worker 任务结束和服务关闭会清理客户端、数据库连接和注册的媒体进程。

## 外部推理与视觉输入

先创建推理 API Key Secret，再发布引用它的 InferenceProfile，以及标签、LabelSet、Prompt、Stage 和 Pipeline。api_secret_reference 填 Secret UUID，不是密钥文本。Profile 的 base_url 应是兼容 /chat/completions 的版本 API 根地址，模型必须支持该 Stage 所需的视觉输入与严格 JSON Schema。

先在 Prompt Test Bench 做受控试跑，再绑定来源 Profile。Preview 只准备输入，不调用外部推理；实际执行会使用同一个 Provider/vault/视觉准备器，不写正式标签、路由或发布记录。实际请求只能在获授权的服务上验收。

媒体 Stage 会从本地归档读取图片和所选视频帧，校验内容与 Hash，再创建实际视觉请求。raw 按真实图片/帧数执行 max_images；adaptive 在超预算时尝试有限拼图；contact_sheet 有明确区域映射。容量仍不足或媒体缺失/损坏时失败关闭，不会悄悄改成文本请求生成正式视觉标签。Manifest v2 记录实际发送次序、内容 Hash、尺寸、目标与帧/区域映射；图片字节不写入普通审计。超过预算的输入以可恢复失败呈现，管理员需调整已发布配置或批次，系统不会截断丢帧。

Pipeline 节点只允许覆盖 timeout_seconds、max_batch_size、max_concurrency、retry_policy 和 visual_composition_policy，不允许通过覆盖更换身份、目标作用域、输入政策或缓存政策。重试子字段在原 Stage 政策上浅合并，再通过领域构造校验。

## 冻结媒体配置与恢复

媒体处理按命中 RangeExecution 冻结的 SourceChannelProfileVersion，解析其 media_processing_profile_version_id。image_max_dimension/webp_quality/video_max_frames 会实际作用于图片和视频证据；当前 active Profile 的后续变化不能改写已冻结执行。已发布后归档/退役的版本继续可被历史执行引用。

无适用执行或冻结配置没有指定媒体版本时，保留明确记录原因的默认回退。明确指定但缺失、非法或多个重叠执行冲突时，不静默选择某个版本；任务持久化失败并保留原因。临时数据库读取异常进入可恢复 retry。已经 READY 的旧资产不会因为新增 Profile 自动改写，需要明确计划重新归档/重分析。

独立 reanalysis 执行完整 DAG，不依赖旧范围仍处于 awaiting 状态；不会自动路由、发布或修改旧 formal run。人工 retry 保留历史尝试，重置本轮预算但审计编号递增；reroute 需要独立显式操作并保留旧发布历史。

## 健康与验收分层

API readiness 检查 PostgreSQL，scheduler/worker readiness 检查 PostgreSQL 和 Redis。外部 Telegram/推理不可用不应让管理员失去排障入口，因此基础 readiness 不代表所有业务能力已就绪。应同时查看身份健康、last_error 类别、失败 Stage/Intent、watermark 和 backlog；没有来源配置时 ingestor 不会假装已连接 Telegram。

普通验收使用真实 PostgreSQL16、Redis/Celery、SQLAlchemy 仓储和生产 factory，只将外部 Telegram 与推理服务作为受控 fixture。独立的本地 TLS/备份恢复演练使用专属 Compose project、卷和主密钥，并清理自己的资源，不触业务项目。

真实 Telegram 发布与真实模型准确率必须另用获授权的测试身份、来源/目的频道和服务配置验收。不要把本地 fixture 的远端结果或任意测试全绿称为真实 Telegram 投递成功；也不要用生产频道试发来替代测试环境。
