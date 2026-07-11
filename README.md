# J.A.R.V.I.S. 自学习助手

基于 Flask、SQLite 和 Anthropic Messages API 的本地智能助手。系统包含分层记忆、行为模式学习、技能沉淀和 Prompt 进化，并通过 Three.js 全息人脸反馈当前认知状态。

## 界面状态

人脸反馈由 `static/js/jarvis-face.js` 独立渲染，不使用随机波浪或音频柱：

| 状态 | 反馈 |
|---|---|
| `idle` | 中性表情、自然眨眼和轻微视线跟随 |
| `listening` | 眼睛张开、虹膜增强，麦克风音量驱动反馈 |
| `thinking` | 视线上移、眉部收紧、扫描速度提高 |
| `speaking` | 嘴唇和下颌随文本输出或语音播报运动 |
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

更多设计背景见 [ARCHITECTURE.md](ARCHITECTURE.md)，部署说明见 [DEPLOY.md](DEPLOY.md)。
