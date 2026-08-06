# 星枢 Nebula — 完整可部署包

> **向量记忆服务 + 黑曜石/Obsidian 笔记（L1）+ 增量同步 + 治理脚本**  
> 目标：`git clone` / `npm` **一条命令**就能在自己的机器跑起来。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

---

## 它是什么

| 层 | 组件 | 作用 |
|----|------|------|
| **L1** | `vault/` 黑曜石笔记（Obsidian） | **权威正文**，人可编辑 |
| **L3** | `vector_memory_server.py` 星枢 API | 语义索引、bootstrap/ask/contract |
| **桥** | `scripts/vault_to_nebula_sync.py` | 笔记 → 向量库增量同步（白名单+体积帽） |
| **治** | scorecard / regression / lifecycle / backup | 可量化健康与回归 |

**不是**聊天记录垃圾场；**不是**密钥库；个人隐私与内网配置不会进仓库。

---

## 一条命令部署（推荐）

### A. Docker（最完整：API + 持久化 + vault 同步守护）

```bash
git clone <repo-url> nebula && cd nebula
cp .env.example .env
# 编辑 .env：填入 BAILIAN_API_KEY
# 若要用自己的 Obsidian 库：VAULT_HOST_PATH=/path/to/your/vault/notes

npm run docker:up
# 等价: docker compose up -d --build

curl -s http://127.0.0.1:26670/v5/health
```

### B. npm / 本机 Python

```bash
git clone <repo-url> nebula && cd nebula
npm run setup          # venv + pip + .env
# 编辑 .env 填 BAILIAN_API_KEY
npm start              # 启动 API
# 另一终端
npm run sync           # 把 vault/notes 同步进星枢
```

### C. 只要二进制式体验

```bash
npx --yes . setup && npx --yes . docker:up
# 在包根目录
```

---

## 黑曜石笔记（必含）

包内自带 **可直接用 Obsidian 打开** 的 vault 模板：

```text
vault/
├── .obsidian/          # 最小配置
├── README.md
└── notes/
    ├── HOME.md         # 总入口
    ├── PROJECT.md
    ├── 00-元信息/ …
    ├── 01-用户画像/ …
    └── 团子学习/ …     # 大体量区，默认同步白名单
```

1. 用 Obsidian：**Open folder as vault** → 选 `vault/`
2. 按 `HOME.md` 写你的知识
3. `npm run sync` 或 Docker 的 `vault-sync` 服务自动每 15 分钟同步

挂载你**已有**的 Obsidian 库：

```bash
# .env
VAULT_HOST_PATH=/home/you/Documents/MyVault/notes
```

```yaml
# docker-compose 已支持 VAULT_HOST_PATH
```

冲突裁决：**笔记原文 > 星枢 vault: chunk > 其它碎片**。

---

## 目录结构

```text
├── vector_memory_server.py   # HTTP API（生产同源脱敏）
├── vector_memory.py / v4/v5  # 检索引擎
├── nebula_secrets.py         # 可选 Vaultwarden 桥（无则降级）
├── vault/                    # 黑曜石/Obsidian 模板
├── scripts/
│   ├── vault_to_nebula_sync.py
│   ├── memory_scorecard.py
│   ├── nebula_regression.py
│   └── backup-nebula.sh
├── deploy/                   # systemd 单元 + install-systemd.sh
├── docker-compose.yml
├── Dockerfile
├── package.json / bin/nebula.js
├── install.sh / start.sh
└── docs/ USAGE*
```

---

## Agent 接入

```
1) POST /v5/bootstrap  {"focus":"主题"}
2) POST /ask           {"query":"...","top_k":5}  → 用 contract/pack
3) vault 命中          → 按 readback 回读 L1 原文
4) POST /memory/add    会话结论（禁密钥）
```

```bash
curl -s http://127.0.0.1:26670/help
```

---

## 环境变量

| 变量 | 说明 |
|------|------|
| `BAILIAN_API_KEY` | **必填** embedding/LLM |
| `BAILIAN_BASE_URL` | 可选，OpenAI 兼容网关 |
| `NEBULA_PORT` | 默认 26670 |
| `VAULT_ROOT` / `VAULT_HOST_PATH` | 笔记目录 |
| `NEBULA_URL` | 同步脚本目标，默认本机 |

完整见 `.env.example`。

---

## systemd（Linux 服务器）

```bash
sudo bash deploy/install-systemd.sh
# 启用：nebula-memory + vault-nebula-sync.timer + lifecycle.timer
```

---

## 验证清单

```bash
npm run health
npm run sync
curl -s -X POST http://127.0.0.1:26670/memory/add \
  -H 'Content-Type: application/json' \
  -d '{"content":"hello nebula","category":"note","importance":0.5}'
curl -s -X POST http://127.0.0.1:26670/ask \
  -H 'Content-Type: application/json' \
  -d '{"query":"hello","top_k":3}'
```

---

## 与 `nebula-memory` 方法论仓库的关系

| 仓库 | 定位 |
|------|------|
| [nebula-memory](https://github.com/123654lkj/nebula-memory) | 契约 / SOP / 离线参考引擎 |
| **本仓库** | **可部署的生产服务器 + 黑曜石同步** |

---



---

## Agent 接入层（hooks + skills · 完整记忆的一半）

只起 API **不等于**记忆系统。本包包含可安装到多 Agent 的门禁与钩子：

```bash
export NEBULA_BASE_URL=http://127.0.0.1:26670
npm run agents:install
# 或 ./agents/install-agents.sh all
# Windows: pwsh -File agents/install-agents.ps1
```

| 组件 | 路径 |
|------|------|
| 五门 + nebula-recall | `agents/skills/` |
| SessionStart bootstrap | `agents/hooks/common/` |
| Grok/Codex/Claude/Cursor/Hermes/Reasonix/OpenClaw | `agents/hooks/<agent>/` |
| OpenCode 插件 | `agents/integrations/opencode/` |
| 通用 AGENTS 片段 | `agents/snippets/AGENTS.memory.md` |

详见 **[agents/README.md](agents/README.md)**。

适配矩阵：Grok · Codex · OpenCode · Claude · Cursor · Hermes · Reasonix · OpenClaw。

## License

MIT — 见 [LICENSE](LICENSE)
