# 星枢 Nebula Server

[![Version](https://img.shields.io/badge/version-5.1.1-green.svg)](CHANGELOG.md)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](requirements.txt)

> **可部署的 L2 向量记忆服务**：HTTP API（`v5.1.1`）+ Obsidian 笔记同步 + 多 Agent hooks/skills  
> `git clone` → 填 key → Docker / systemd / 本机 Python 一条命令跑起来。

**不是**聊天日志垃圾场 · **不是**密钥库 · 不含内网地址与个人数据。

---

## 快速部署

### A. Docker（推荐：API + 数据卷 + vault 定时同步）

```bash
git clone https://github.com/123654lkj/nebula-server.git
cd nebula-server
cp .env.example .env
# 编辑 .env：填入 BAILIAN_API_KEY（百炼/DashScope）
# 可选：VAULT_HOST_PATH=/path/to/your/obsidian/notes

docker compose up -d --build
# 或: npm run docker:up

curl -s http://127.0.0.1:26670/v5/health
curl -s http://127.0.0.1:26670/help
```

### B. 本机 Python

```bash
git clone https://github.com/123654lkj/nebula-server.git
cd nebula-server
./install.sh                 # venv + 依赖 + .env
# 编辑 .env 填 BAILIAN_API_KEY
./start.sh                   # 前台
# 或: npm run setup && npm start
```

### C. systemd（Linux 服务器）

```bash
sudo bash deploy/install-systemd.sh
# 默认装到 /opt/nebula；可改: NEBULA_INSTALL_DIR=/srv/nebula sudo -E bash deploy/install-systemd.sh
```

启用：`nebula-memory` + `vault-nebula-sync.timer` + `nebula-lifecycle.timer`。

---

## 它是什么

| 层 | 组件 | 作用 |
|----|------|------|
| **L1** | `vault/` Obsidian 笔记模板 | 权威正文，人可编辑 |
| **L2** | `vector_memory_server.py` | 语义索引、`bootstrap` / `ask` / `contract` |
| **桥** | `scripts/vault_to_nebula_sync.py` | 笔记 → 向量库增量同步（白名单 + 体积帽） |
| **治** | scorecard / regression / backup / lifecycle | 健康与回归 |
| **Agent** | `agents/hooks` + `agents/skills` | SessionStart + 五门门禁 |

---

## Agent 标准用法

```text
1) POST /v5/bootstrap  {"focus":"主题"}     → 注入 bootstrap
2) POST /ask           {"query":"...","top_k":5}
      → 优先 contract → pack；勿 dump 全文 results
3) trust=canon/source 且 executable → 可执行
4) 有 readback → 回读 vault 原文
5) 重要结论 → POST /memory/add（禁止密钥）
```

```bash
curl -s http://127.0.0.1:26670/help              # mini（默认）
curl -s 'http://127.0.0.1:26670/help?level=short'
curl -s -X POST http://127.0.0.1:26670/ask \
  -H 'Content-Type: application/json' \
  -d '{"query":"你的问题","top_k":5}'
```

安装 Agent 钩子：

```bash
export NEBULA_BASE_URL=http://127.0.0.1:26670
npm run agents:install
# 或: ./agents/install-agents.sh all
```

详见 [agents/README.md](agents/README.md)。

---

## 目录结构

```text
nebula-server/
├── vector_memory_server.py   # HTTP API
├── vector_memory.py / nebula_v4.py / nebula_v5.py
├── nebula_secrets.py         # 可选 Vaultwarden 桥
├── vault/                    # Obsidian 模板
├── scripts/                  # sync / scorecard / regression / backup
├── deploy/                   # systemd + install-systemd.sh
├── agents/                   # hooks + skills + OpenCode 插件
├── docs/USAGE*.md            # /help 同源文档
├── docker-compose.yml
├── Dockerfile
├── install.sh / start.sh
└── package.json / bin/nebula.js
```

---

## 环境变量

| 变量 | 说明 |
|------|------|
| `BAILIAN_API_KEY` | **必填** embedding / LLM |
| `BAILIAN_BASE_URL` | 可选 OpenAI 兼容网关 |
| `NEBULA_HOST` / `NEBULA_PORT` | 默认 `0.0.0.0:26670` |
| `NEBULA_DB_PATH` | SQLite 路径，默认 `./data/memory_vectors.db` |
| `VAULT_ROOT` / `VAULT_HOST_PATH` | 笔记目录 |
| `NEBULA_URL` | 同步脚本目标，默认本机 |

完整见 [`.env.example`](.env.example)。

---

## 验证

```bash
npm run health
npm run sync
curl -s -X POST http://127.0.0.1:26670/memory/add \
  -H 'Content-Type: application/json' \
  -d '{"content":"hello nebula","category":"note","importance":0.5}'
curl -s -X POST http://127.0.0.1:26670/ask \
  -H 'Content-Type: application/json' \
  -d '{"query":"hello","top_k":3,"llm_deep":"off"}'
```

更多：[`VERIFY.md`](VERIFY.md) · [`docs/USAGE.md`](docs/USAGE.md)

---

## 与姊妹仓库

| 仓库 | 定位 |
|------|------|
| **[nebula-server](https://github.com/123654lkj/nebula-server)**（本仓） | **可部署生产服务** + vault 同步 + Agent 接入 |
| [nebula-memory](https://github.com/123654lkj/nebula-memory) | 契约 / SOP / 离线参考引擎 |
| [agent-memory-methodology](https://github.com/123654lkj/agent-memory-methodology) | L0–L5 全栈记忆方法论 |

---

## License

MIT — 见 [LICENSE](LICENSE)
