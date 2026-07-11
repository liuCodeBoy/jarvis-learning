# 部署说明

## 本地运行

要求 Python 3.10+、支持 FTS5 的 SQLite，以及可选的麦克风权限。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY='your-key'
python3 start.py --host 127.0.0.1 --port 8000
```

模型代理可配置：

```bash
export ANTHROPIC_AUTH_TOKEN='your-token'
export ANTHROPIC_BASE_URL='https://gateway.example/anthropic'
export ANTHROPIC_MODEL='provider-model-name'
export ANTHROPIC_TOTAL_TIMEOUT_SECONDS=95
```

服务启动不会探测或调用模型。`GET /health` 只检查数据库，模型可用状态通过 `GET /api/status` 返回。

## Docker Compose

```bash
export ANTHROPIC_API_KEY='your-key'
export JARVIS_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export GRAFANA_ADMIN_PASSWORD='replace-this-password'
export JARVIS_UID="$(id -u)"
export JARVIS_GID="$(id -g)"
docker compose up --build -d
docker compose ps
```

默认端口仅绑定本机：

| 服务 | 地址 |
|---|---|
| J.A.R.V.I.S. | `127.0.0.1:8000` |
| Grafana | `127.0.0.1:3000` |
| Prometheus | `127.0.0.1:9090` |
| Pushgateway | `127.0.0.1:9091` |

需要在受信网络监听时设置 `JARVIS_BIND_HOST=0.0.0.0`，并且必须设置非空 `JARVIS_API_TOKEN`；只有上游认证而没有 Jarvis 令牌仍会被 API Host 边界拒绝。不要直接把 Flask/Gunicorn 端口暴露到公网；应在上游反向代理终止 TLS，并配置身份认证、请求体上限和访问日志。
TLS 反向代理还必须设置公开 Origin，供写请求做来源校验：

```bash
export JARVIS_PUBLIC_ORIGIN='https://jarvis.example.com'
export JARVIS_TRUSTED_PROXY_HOPS=1
```

该值必须只包含 scheme、主机和可选端口，不带路径或末尾斜杠。
`JARVIS_TRUSTED_PROXY_HOPS` 只应在应用端口无法绕过可信代理访问时启用；
数值必须等于实际可信代理跳数，否则客户端可伪造来源 IP。

## 持久化

Compose 挂载以下目录：

- `./data` -> SQLite 数据库
- `./logs` -> 应用和定时任务日志
- `./backups` -> 在线备份

容器以 `JARVIS_UID:JARVIS_GID` 身份运行，避免为了写 bind mount 而使用
root。`scripts/deploy.sh` 会自动使用当前宿主用户；直接运行 Compose 时应按
上面的示例显式设置。

容器首次启动会自动创建完整 Schema，不依赖仓库内的开发数据库。在线备份接口使用 SQLite Backup API：

```bash
curl -X POST \
  -H "X-Jarvis-Token: $JARVIS_API_TOKEN" \
  http://127.0.0.1:8000/api/backup
```

灾备工具使用 SQLite 在线备份、SHA-256 清单和原子恢复：

```bash
python3 scripts/disaster_recovery.py create
python3 scripts/disaster_recovery.py list
python3 scripts/disaster_recovery.py verify BACKUP_ID
python3 scripts/disaster_recovery.py restore BACKUP_ID --confirm
```

恢复会拒绝仍被其他连接占用的数据库，并先创建 pre-restore 备份。配置文件只随备份保存供人工核对，不会在恢复数据库时自动覆盖。

## 数据库迁移与回滚

正常升级不需要手工执行 SQL。服务启动时会幂等创建完整运行时 Schema，并为
旧表补齐 `sessions` 学习字段、`evolution_history.approved` 和
`eval_cases.interaction_id`；评估样本与交互之间还有唯一索引，防止同一回复被
重复加入进化样本。交互和反馈都通过外键绑定所属会话；升级旧数据库时，找不到
原会话的历史交互会被挂到 `platform='legacy'` 的专用归档会话，避免为了补
外键而删除历史数据。

当前只有短期记忆保留并同步 FTS 索引。交互记录和知识节点没有独立 FTS 表。
旧版本中从未被同步读取的 `interactions_fts`、`interactions_fts_trigram`、
`knowledge_fts`、`knowledge_fts_trigram` 会在启动时清理。知识节点、边及实体
普通索引仍会保留。
长期记忆使用当前用户命名空间内的有界关键词检索，不保存伪语义向量。旧库没有
实际向量数据时，升级会保留记忆行并重建 `long_term_memory`，同时删除空的
`embedding_index`；如果检测到非空旧向量，初始化会保留原表，避免隐式丢失数据。

`scripts/migration.sql` 是一个冻结的版本 1 Schema 快照，只支持从“数据库中仅有
九列基础 `sessions` 表”的旧版基线做一次性离线迁移。它不是通用迁移工具，也
不能在已经初始化的数据库上重复执行；前置检查会拒绝未知或已迁移的 Schema。
确实需要转换该旧版基线时，先停止所有应用进程并创建备份，再使用 SQLite CLI
的 fail-fast 模式：

```bash
sqlite3 data/jarvis_learning.db \
  ".backup 'backups/pre-schema-v1.db'"
sqlite3 -bail data/jarvis_learning.db < scripts/migration.sql
sqlite3 data/jarvis_learning.db \
  'PRAGMA foreign_keys=ON; PRAGMA foreign_key_check; PRAGMA integrity_check;'
```

`foreign_key_check` 应无输出，`integrity_check` 应输出 `ok`。

`scripts/rollback.sql` 只用于紧接该迁移后的应急回退，会删除全部学习、交互、
反馈、Skill、记忆和 FTS 数据，仅保留旧版 `sessions` 的九个基础字段。需要保留
应用数据时应恢复已验证备份，而不是运行该破坏性脚本。确认接受数据丢失后才可
离线执行：

```bash
sqlite3 -bail data/jarvis_learning.db < scripts/rollback.sql
```

## 验证

```bash
curl --fail http://127.0.0.1:8000/health
curl -H "X-Jarvis-Token: $JARVIS_API_TOKEN" \
  http://127.0.0.1:8000/api/status
docker compose config --quiet
pytest -q
python3 scripts/security_audit.py
```

## 生产检查

- 使用长随机 `JARVIS_API_TOKEN`，定期轮换模型密钥。
- 反向代理启用 HTTPS，不提交证书私钥。
- 修改 Grafana 初始密码，不向公网暴露监控端口。
- 限制 `data/`、`logs/`、`backups/` 的文件系统权限。
- 为备份配置异机副本和恢复演练。
- 监控 `/health`、进程退出、磁盘空间、SQLite WAL 大小和模型错误率。
