# J.A.R.V.I.S. 自学习助手

基于 Flask、SQLite 和 Anthropic Messages API 的本地智能助手。系统包含分层记忆、行为模式学习、技能沉淀、受控本机工具和 Prompt 进化，并通过 Three.js 分片式机械面甲反馈当前认知状态。模型回答经 SSE/NDJSON 流式输出；启用 Azure Speech 后，文字、口周甲片和铰接下颌由同一音频时钟推进。

## 界面状态

人脸反馈由 `static/js/jarvis-face.js` 独立渲染，不使用随机波浪或音频柱：

| 状态 | 反馈 |
|---|---|
| `idle` | 中性表情、自然眨眼和轻微视线跟随 |
| `listening` | 眼睛张开，瞳孔由麦克风音量驱动 |
| `thinking` | 视线上移，面罩切换为琥珀色并轻微倾斜 |
| `speaking` | 播放器按音频时钟消费 22 类标准 viseme，驱动刚性口周甲片和铰接下颌 |
| `executing` | 专注表情，用于学习、检索和数据操作 |
| `error` | 红色面部反馈并自动恢复待命 |

## 本地启动

要求 Python 3.10+。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export ANTHROPIC_API_KEY='your-key'
# 代理网关可同时设置 ANTHROPIC_BASE_URL 和 ANTHROPIC_MODEL
# 单次模型调用（含重试）的总预算默认 95 秒，最高限制为 100 秒

python3 start.py
```

如果使用原生 Anthropic API，不要设置代理地址；如果使用兼容 Anthropic
协议的网关，必须同时使用该网关签发的密钥和支持的模型名：

```bash
# 原生 Anthropic
unset ANTHROPIC_BASE_URL
export ANTHROPIC_API_KEY='sk-ant-...'

# PPInfra 示例
export ANTHROPIC_BASE_URL='https://api.ppinfra.com/anthropic'
export ANTHROPIC_API_KEY='ppinfra-key'
export ANTHROPIC_MODEL='provider-supported-model'

# 讯飞 MaaS / Claude Code 配置示例
export ANTHROPIC_BASE_URL='https://maas-coding-api.cn-huabei-1.xf-yun.com/anthropic'
export ANTHROPIC_AUTH_TOKEN='provider-token'
export ANTHROPIC_MODEL='astron-code-latest'
```

界面提示“模型凭据无效或无权限”对应上游 HTTP 401/403，表示密钥被供应商
拒绝，需要在供应商后台轮换密钥后重启服务；这不是浏览器或数据库故障。

浏览器访问 <http://127.0.0.1:8000>。默认只监听本机；需要局域网访问时显式传入 `--host 0.0.0.0`，并设置 `JARVIS_API_TOKEN`。

首次启动会幂等初始化当前数据库 Schema，常规升级无需手工运行 SQL。仅从旧版
九列基础 `sessions` 数据库迁移时，才使用部署说明中的一次性离线脚本；详见
[数据库迁移与回滚](DEPLOY.md#数据库迁移与回滚)。

```bash
export JARVIS_API_TOKEN='a-long-random-token'
python3 start.py --host 0.0.0.0 --port 8000
```

通过 TLS 反向代理提供服务时，另设
`JARVIS_PUBLIC_ORIGIN='https://jarvis.example.com'`，否则浏览器写请求会因
Origin 与内部 HTTP Host 不一致而被拒绝。

其他入口：

```bash
python3 start.py --mode local
python3 start.py --test
python3 start.py --test-report
```

自动学习默认按 `config.yaml` 关闭，避免每轮对话额外消耗模型额度。启用时同时设置总开关和需要的子模块，或使用对应环境变量覆盖：

```bash
export JARVIS_LEARNING_ENABLED=true
export JARVIS_KNOWLEDGE_EXTRACTION_ENABLED=true
export JARVIS_EVOLUTION_ENABLED=true
export JARVIS_SKILL_MINING_ENABLED=true
```

本机个人覆盖配置可放在 `config.local.yaml` 或 `config.local.yml`，这两个文件已被
`.gitignore` 排除，不会随 Git 提交。模型凭据优先从环境变量读取；本机开发时也会
读取 `config.local.yaml` 和 `~/.claude/settings.json` 的 `env` 字段，不要把本地
配置提交到 Git。

## 流式语音与口型

浏览器 `SpeechSynthesisUtterance` 不提供音素或 viseme，只提供词/句边界，因此不能
用于可靠的中文口型同步。本项目把模型 SSE 转为 NDJSON 增量文本，前端按标点组成短句并
预取 TTS。播放开始后，前端以 `AudioContext.currentTime` 同时推进该句文字和刚性甲片
口型。`auto` 模式优先使用 Azure 返回的 22 类精确 viseme；没有 Azure 密钥时使用
Edge TTS，并直接分析实际播放音频的能量和频谱来驱动甲片，不按文字猜口型。两种服务都
不可用时，文字仍直接流式显示，但不会制造假语音或假口型。

在被 Git 忽略的 `config.local.yaml` 中加入自己的 Azure Speech 资源信息：

```yaml
voice:
  provider: "auto"
  azure:
    key: "your-azure-speech-key"
    region: "your-resource-region"
    voice: "zh-CN-YunxiNeural"
  edge:
    voice: "zh-CN-YunxiNeural"
    rate: "+0%"
    pitch: "+0Hz"
```

也可以使用环境变量：

```bash
export AZURE_SPEECH_KEY='your-azure-speech-key'
export AZURE_SPEECH_REGION='your-resource-region'
export AZURE_SPEECH_VOICE='zh-CN-YunxiNeural'
```

`ANTHROPIC_AUTH_TOKEN` 只能调用语言模型，不能代替 Azure Speech 凭据。默认 `auto`
会在 Azure 未配置时使用无需密钥但仍需联网的 Edge TTS；设置
`JARVIS_VOICE_PROVIDER=azure` 可强制只使用精确 Azure viseme。修改后重启服务，
`GET /api/status` 中的 `speech.available` 应为 `true`。完整选型依据见
[语音口型与人脸方案](docs/voice-face-options.md)。

## Docker

```bash
export ANTHROPIC_API_KEY='your-key'
export JARVIS_API_TOKEN='a-long-random-token'
export JARVIS_UID="$(id -u)"
export JARVIS_GID="$(id -g)"
docker compose up --build -d
```

核心界面默认映射到 `127.0.0.1:8000`。Prometheus 和 Grafana 分别位于本机 `9090`、`3000`；对外暴露时应通过具备 TLS 和身份认证的反向代理。
`scripts/deploy.sh` 会自动设置宿主用户的 UID/GID；直接运行 Compose 时
显式设置这两个值，可确保非 root 容器能够写入绑定的 `data`、`logs` 和
`backups` 目录。

## 测试

```bash
pip install -r requirements-dev.txt
pytest -q
```

测试覆盖 LLM 配置安全、Prompt 组合、单条交互持久化、中文 FTS、记忆淘汰、会话隔离、核心算法和 Web API 契约。

常用运维检查：

```bash
python3 scripts/security_audit.py
python3 scripts/capacity_planning.py
python3 scripts/disaster_recovery.py create
python3 scripts/performance_benchmark.py
```

## 目录

```text
jarvis_learning/
├── start.py
├── jarvis/
│   ├── api/web_app.py          # Flask 应用工厂和 API
│   ├── core/llm.py             # 模型客户端
│   ├── voice/                  # 音频与 viseme 同源的 TTS 适配器
│   ├── database/               # Schema 与数据访问
│   ├── learning/               # 模式、技能和进化
│   └── memory/                 # 分层记忆与对话桥接
├── templates/index.html        # 页面语义结构
├── static/
│   ├── css/app.css             # 响应式界面
│   ├── js/state-machine.js     # 显式状态机
│   ├── js/jarvis-face.js       # Three.js 人脸组件
│   ├── js/api-client.js        # API 错误/超时契约
│   ├── js/app.js               # 对话与模块控制器
│   └── vendor/README.md        # 固定前端依赖版本与校验值
├── tests/                      # pytest 回归测试
├── monitoring/                 # Prometheus / Grafana
└── docker-compose.yml
```

## 安全约束

- 源码不包含 API 密钥，凭据只从环境变量读取。
- Web 默认绑定 `127.0.0.1`；非回环 API 必须设置 `JARVIS_API_TOKEN`，公网还应增加上游 TLS 和身份认证。
- 会话和浏览器用户 ID 均由服务端随机生成；历史按会话隔离，记忆按浏览器用户隔离。
- 动态内容只以文本节点渲染，模型和记忆内容不会直接进入 `innerHTML`。
- 自动挖掘的 Skill 默认禁用，需要审核后启用。
- 助手回复只有在用户明确标记“有用”后才成为进化参考样本。
- API 包含输入大小限制、频率限制、来源检查和安全响应头。
- 本机工具通过模型的结构化工具调用执行，支持目录列表、文本读取、目录创建、文本写入和用默认应用打开文件；不执行任意 Shell，并拒绝工作区外路径。
- 工具工作区默认是 `~/Desktop`，可在被 Git 忽略的 `config.local.yaml` 中设置 `tools.workspace_path`，或用 `JARVIS_WORKSPACE_PATH` 临时覆盖。
- macOS 需要给实际启动服务的终端或 Python 应用开启“桌面与文稿文件夹”权限；每次操作都以工具返回的真实结果为准，失败时不会报告成功。

更多设计背景见 [ARCHITECTURE.md](ARCHITECTURE.md)，部署说明见 [DEPLOY.md](DEPLOY.md)。
