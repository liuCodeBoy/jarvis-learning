# J.A.R.V.I.S. 架构说明

版本：4.2

## 总览

```text
Browser
  ├── explicit state machine
  ├── Three.js segmented mechanical mask
  ├── Web Audio viseme scheduler
  ├── chat / learning / memory / skill views
  └── typed API client + NDJSON stream reader
             │ HTTP/JSON + NDJSON
             ▼
Flask application factory
  ├── session and input boundary
  ├── chat orchestration
  ├── guarded local command executor
  ├── speech synthesis adapter
  ├── memory namespace
  ├── skill matching
  ├── health / metrics / backup
  └── background learning triggers
             │
       ┌─────┴──────────────┐
       ▼                    ▼
Anthropic-compatible LLM   SQLite (WAL + FTS5)
Azure Speech (optional)
```

系统是单节点、本地优先的应用。SQLite、进程内限流和后台线程均以单实例部署为基准；需要水平扩展时，应先把限流、任务锁、会话所有权和学习任务迁移到共享基础设施。

## 前端边界

前端不再内嵌在 Python 字符串中：

| 文件 | 职责 |
|---|---|
| `templates/index.html` | 语义结构、可访问标签和模块容器 |
| `static/css/app.css` | 响应式工具界面和稳定布局 |
| `static/js/state-machine.js` | 状态、操作 ID 和过期操作隔离 |
| `static/js/jarvis-face.js` | Three.js 人脸几何、表情和帧循环 |
| `static/js/api-client.js` | JSON/NDJSON 契约、超时、取消、认证和错误类型 |
| `static/js/app.js` | 对话、语音、模块视图与安全 DOM 渲染 |

### 人脸状态机

```text
idle ── microphone open ──> listening
  │                            │ final transcript
  └──── text submit ───────> thinking
                                  │ response
                                  ▼
                               speaking ──> idle

module action: any -> executing -> idle
failure:       any -> error -> idle
```

每次异步操作获得递增的 `operationId`。旧请求、旧 TTS 回调和旧定时器不能覆盖新操作状态。麦克风开启时，真实 RMS 音量输入驱动瞳孔反馈；语音播报时，模型增量文本按句进入 TTS 预取队列。Azure 可用时后端返回同源音频和 viseme offset；否则 Edge 返回语音，浏览器从正在播放的音频提取能量和频谱。两条路径都以 `AudioContext.currentTime` 同时推进当前句的文字显示和口型，文字打字速度不再独立驱动嘴部。
系统启用 `prefers-reduced-motion` 时停止漂移和眨眼脉冲；作为内容反馈的真实 viseme 和音量输入仍保留。

`JarvisFace` 只暴露 `setState`、`setAudioLevel`、`startSpeaking`、`setViseme`、`stopSpeaking`、`setWorkspaceOpen`、`resize` 和 `destroy`，业务控制器不直接操作 Three.js Mesh。带折角的核心轮廓、分离装甲板、深眼眶和铰接式口周甲片均由组件在同一个局部坐标系中生成。viseme 的开合量与宽度只驱动甲片的刚性位移、偏航、俯仰和下颌铰链；不存在独立口腔网格、嘴线图层或基于未知模型坐标的叠加嘴片。

## API 边界

`jarvis/api/web_app.py` 使用 `create_app()` 创建应用。统一响应结构：

```json
{"ok": true, "data": {}}
```

```json
{"ok": false, "error": {"code": "invalid_session", "message": "..."}}
```

关键约束：

- 新会话由 `POST /api/session` 生成 128 位随机 ID。
- 消息、记忆键和值均有服务端长度限制。
- `POST /api/chat/stream` 把 Anthropic SSE 转为 `start`、`delta`、`reset`、`done`、`error` NDJSON 事件；旧 `POST /api/chat` JSON 契约继续保留。
- `POST /api/speech` 要求有效会话，单段最多 500 字；Azure 返回 PCM WAV 与同源 viseme 时间轴，Edge 后备返回 MP3 与 `audio-analysis` 标记。
- API 错误使用对应的 4xx/5xx 状态，不把异常伪装成 HTTP 200。
- 同一会话只允许一个模型请求在途；重叠请求返回 `409 session_busy`，避免多标签页基于同一旧历史重复付费调用。
- 回环地址可不设令牌；所有非回环 API 都要求 `JARVIS_API_TOKEN`，避免 tokenless 本地服务遭 DNS rebinding。
- 写请求检查浏览器 `Origin`，高成本接口使用进程内滑动窗口限流。
- TLS 代理通过显式 `JARVIS_PUBLIC_ORIGIN` 校验来源；仅在配置可信代理跳数后才接受转发 IP。
- API 响应禁用缓存，并设置 CSP、Frame、MIME 和权限策略。
- 模型、记忆和 Skill 内容在浏览器中只以文本节点渲染。

`/health` 不调用外部模型，只验证本地数据库；`/metrics` 暴露 Prometheus 指标；`/api/backup` 使用 SQLite 在线备份 API。`POST /api/feedback` 绑定会话和 interaction，拒绝跨会话标注。

## 对话流

```text
1. 校验消息与服务端会话
2. 获取该会话的进程内请求门；已有请求在途时返回 409
3. 读取该会话最近 10 轮历史
4. 写入 pending interaction
5. 在该会话的记忆命名空间中做本地键/关键词检索
6. 匹配已审核且启用的 Skill
7. 以 Anthropic SSE 调用 LLM，并把文本增量立即转发为 NDJSON；若返回 `tool_use`，执行注册工具并通过 `tool_result` 继续对话
8. 浏览器按标点组成不超过 220 字的语音片段，最多预取两段 TTS
9. 每段音频开始后，以同一个音频时钟推进可见文字和 viseme 甲片动画
10. 完整响应持久化后发送 `done`，再以 best-effort 更新 Skill 统计并触发后台学习任务
```

模型的主 System Prompt 与历史记忆严格区分。记忆被放入标记为“不可信数据”的用户内容块，不能覆盖 System Prompt。多个受信 System 消息按顺序合并，不再只保留最后一条。

系统日期查询作为无副作用的确定性命令在模型调用前处理。文件操作通过
`jarvis/tools/host_tools.py` 暴露为 Anthropic 结构化工具，支持目录列表、文本读取、
目录创建、原子文本写入和使用默认应用打开文件。所有路径解析后必须位于配置的工作区
内，路径穿越和符号链接逃逸会被拒绝。工具未返回 `ok: true` 时，最终回复不能声称操作
成功；若后续模型请求失败，则回复会列出已经执行的步骤并明确任务没有全部确认完成。
系统不提供任意 Shell 工具。

## 记忆隔离

记忆系统包含：

| 层级 | 存储 | 用途 |
|---|---|---|
| immediate | 进程内 LRU + TTL | 当前热点 |
| short-term | SQLite + FTS5 | 可搜索的近期数据 |
| long-term | SQLite | 稳定事实和偏好 |

服务端为浏览器签发独立的随机用户 ID，新会话可以复用该 ID。用户 ID 经 SHA-256 派生为内部前缀，真实键格式为 `user:<hash>:<key>`。历史按会话隔离，长期记忆按浏览器用户隔离；精确检索和模糊检索都必须匹配同一前缀。

召回不再在主回答前额外调用模型。常见姓名、年龄、位置、偏好和日程问题映射到稳定键，其余内容走当前命名空间内的关键词搜索；因此模型请求预算只用于最终回答。

长期记忆不声明或存储未实现的语义向量。旧库没有实际向量数据时，升级会无损重建 `long_term_memory` 并清理空的 `embedding_index`；如果检测到非空旧向量，则保留原表，避免自动升级破坏未知数据。

短期记忆更新时先删除旧 FTS 文档再建立索引，删除与容量淘汰同步清理 FTS。JSON 使用 `ensure_ascii=False`，保证中文可以被 FTS/LIKE 搜索。

## 学习与进化

- `POST /api/learn` 分析最近 30 天真实对话意图；每会话最多 50 条，模式深度最多 5，避免递归失控。
- `PrefixSpan` 使用真实序列支持度和条件置信度，阈值计数向上取整。
- `FTRLOnlineLearning` 使用标准 per-coordinate `sigma` 更新。
- 自动挖掘的 Skill 初始为 disabled；模型生成正则不执行，启用前必须在界面展开模板和关键词并明确审核。
- Prompt 进化必须提供 LLM evaluator；无评估器时直接拒绝运行。
- `fitness_score` 表示质量，新颖性只写入 `selection_score`，不污染达标判断。
- 只有用户明确标记“有用”的回答才成为 `expected`；负反馈不会伪造参考答案。
- 重复提交相同反馈不会重复写反馈记录，也不会把已消费的样本重新投放；反馈值真正改变时才重新进入候选集。
- 每轮最多使用 6 条已标注样本、2 代和 1 个变体；精英评分不会重复调用模型，完整两代最多 25 次模型调用、总时长 5 分钟；成功后才标记已使用，失败至少退避 1 小时再重试。

当前学习任务仍由进程内后台线程触发。这适合个人单节点运行；生产多副本应改为独立 worker 和带租约的任务队列。

## 数据库

所有运行路径通过 `JARVIS_DB_PATH` 选择数据库，默认是项目内 `data/jarvis_learning.db`。首次启动自动创建 Schema、WAL、短期记忆 FTS、记忆、Skill 和评估表。完整 Web 运行时 Schema 由 `LearningDatabaseSchema`、记忆组件和 `SkillStore` 共同初始化。

`users` 是用户外键根；`sessions.user_id`、学习状态和偏好表均引用 `users.id`。交互和反馈通过 `session_id` 引用会话，反馈和评估样本通过 `interaction_id` 引用原始交互。连接开启 `foreign_keys=ON`。Schema 初始化是幂等的，并通过 `PRAGMA table_info` 为旧 `sessions`、`evolution_history` 和 `eval_cases` 补列；旧版孤立交互会被保留并归入 `platform='legacy'` 的专用归档会话。`evolution_history.approved` 控制 Prompt 候选是否获准使用；`eval_cases.interaction_id` 通过部分唯一索引绑定原始交互，避免重复收集同一回复。

FTS 只服务短期记忆，其写入、更新、删除和容量淘汰都会同步维护索引。交互和知识节点不再维护没有运行时读取者的搜索副本；初始化会删除旧版本遗留的 `interactions_fts`、`interactions_fts_trigram`、`knowledge_fts` 和 `knowledge_fts_trigram`。知识节点、边及实体普通索引仍保留。

仓库内 `scripts/migration.sql` 只是从九列基础 `sessions` 表迁移到当前运行时 Schema 的一次性版本 1 快照，`scripts/rollback.sql` 则是会删除所有新增应用数据的对应回退。常规部署和升级依赖上述幂等初始化；静态脚本的精确基线、备份要求和验证命令见 [DEPLOY.md](DEPLOY.md#数据库迁移与回滚)。

## 部署

本地开发默认监听 `127.0.0.1:8000`。导入 `web_app` 只暴露应用工厂，不会打开默认数据库；Gunicorn 通过独立 `jarvis.api.wsgi` 入口创建实例。Docker 使用单 worker/多线程，以保持进程内会话门、学习锁和限流语义一致。普通模型请求含重试的总预算限制为 100 秒以内，浏览器的模型接口超时为 110 秒，Gunicorn 为 130 秒。Compose 提供：

- J.A.R.V.I.S. core
- Prometheus
- Pushgateway
- Grafana
- cron scheduler

所有宿主端口默认绑定 `127.0.0.1`。公网部署需要独立 TLS/认证反向代理；仓库不生成或提交私钥。核心与 cron 容器使用宿主 UID/GID 写 bind mount，不以 root 运行。

## 后续演进

在增加团队用户或水平扩展前，建议按顺序完成：

1. 引入正式用户认证，将会话命名空间绑定到账户而不是浏览器会话。
2. 将学习、进化和 Skill mining 移入独立任务队列。
3. 用 Redis 等共享存储替换进程内会话门、限流和任务冷却时间。
4. 增加 Prompt 版本回滚和负反馈修订答案流程。
5. 为 Schema 引入版本化迁移工具，而不是继续扩展单一初始化函数。
