# 星枢 Nebula — 个人/家庭向量记忆系统

> 让 AI 记住一切：跨会话、跨 Agent 的语义记忆 + 检索 + 治理，单文件 SQLite 存储，一条命令启动。

星枢（Nebula）是一套**自托管的向量记忆服务**：把任何文本（笔记、会话、文档、日志）embedding 后存入 SQLite，通过 REST/MCP 接口提供语义检索、可信度分层（trust）、会话注入（bootstrap）、自动治理（supersede/压缩/降权）等能力。设计目标：**AI Agent 的长期记忆层**，个人部署、无外部向量数据库、可备份可迁移。

## 亮点

- **零外部存储依赖**：向量直接存 SQLite（BLOB + numpy 内存矩阵），无 faiss/redis/pgvector，备份 = 拷一个文件
- **多引擎**：`ultimate`（v5，图+多跳+LLM 裁决）/ `reflect`（v4，反思检索）/ 混合检索（向量 + 关键词）
- **可信度分层**：canon / source / synthesis / hearsay / superseded，带权威衰减与自动降权
- **省 token 设计**：`contract`（裁决短文）+ `pack`（紧凑证据），Agent 按需取用，不为全文烧 token
- **可选增强**：本地 reranker 重排（bge-reranker-v2-m3）、Vaultwarden 密钥桥、MCP 工具注册——都不装也能跑核心功能
- **治理自动化**：去重、压缩、过时标记（supersede）、生命周期降权、回归自检

## 快速开始

### 1. 准备

- Python 3.10+
- 一个百炼（DashScope）API key（embedding + LLM 改写用），或在环境变量指定兼容 OpenAI 格式的端点

### 2. 安装

```bash
git clone <repo-url> nebula && cd nebula
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 3. 配置

```bash
export BAILIAN_API_KEY=sk-xxx            # 必填：embedding/LLM 走百炼
# 可选：
export NEBULA_LLM_MODEL=qwen3.7-plus     # LLM 模型（默认 qwen3.7-plus）
export NEBULA_PORT=26670                 # 服务端口
export NEBULA_HOST=0.0.0.0               # 监听地址
export NEBULA_DB_PATH=./data/memory_vectors.db  # 数据库位置（默认同目录 data/）
```

兼容任意 OpenAI 格式网关：

```bash
export BAILIAN_BASE_URL=https://your-gateway/v1
```

### 4. 启动

```bash
python3 vector_memory_server.py
```

或一键脚本：`./install.sh`（自动装依赖 + 生成 systemd 单元 + 开机自启）。

### 5. 验证

```bash
curl -s http://127.0.0.1:26670/v5/health
# → {"version":"v5.0-ultimate","total_active":0,...}（新库为空）
```

## Agent 接入（标准流程）

```
1) 接任务  → POST /v5/bootstrap {"focus":"主题"}     # 会话开场注入
2) 提问    → POST /ask {"query":"...","top_k":5}     # contract→pack→results
3) 写入    → POST /memory/add {"content","category","importance"}
4) 收尾    → POST /v5/session-extract {transcript,focus}  # 会话精华回写
```

MCP 客户端：`python3 vector_memory_server.py` 同时暴露 `/mcp`（JSON-RPC）端点，或直接 import 工具函数。

## 环境变量一览

| 变量 | 默认 | 说明 |
|------|------|------|
| `BAILIAN_API_KEY` | — | **必填**，百炼/DashScope API key |
| `BAILIAN_BASE_URL` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | OpenAI 兼容端点 |
| `NEBULA_LLM_MODEL` | `qwen3.7-plus` | LLM 模型（改写/裁决/压缩） |
| `NEBULA_HOST` / `NEBULA_PORT` | `0.0.0.0` / `26670` | 监听地址端口 |
| `NEBULA_DB_PATH` | `./data/memory_vectors.db` | 数据库路径 |
| `NEBULA_VAULT_ROOT` | 空 | 本地知识库根目录（启用 `readback` 原文回读提示） |
| `NEBULA_CA_PATH` | 空 | 自定义 CA 证书（自签 HTTPS 网关用） |
| `NEBULA_ARK_CONFIG` | 空 | 火山方舟配置文件路径（可选） |
| `BW_SESSION_FILE` | 空 | Vaultwarden session 文件（可选密钥桥） |

## 目录结构

```
nebula/
├── vector_memory_server.py   # REST + MCP 服务入口（Flask）
├── vector_memory.py          # 核心：存储/检索/治理（87KB）
├── nebula_v5.py              # ultimate 引擎（图+多跳+LLM 裁决）
├── nebula_v4.py              # reflect 引擎 + 迁移/回填
├── reranker.py               # 可选：本地 bge-reranker-v2-m3
├── nebula_secrets.py         # 可选：Vaultwarden 密钥桥
├── nebula_session_extract.py # 会话精华抽取/分层召回
├── docs/                     # API 文档（/help 同源）
├── data/                     # SQLite 数据库（运行时生成，备份此目录即可）
└── install.sh                # 一键部署（venv+依赖+systemd）
```

## 部署为 systemd 服务

```bash
sudo ./install.sh            # 生成 /etc/systemd/system/nebula.service 并 enable --now
sudo systemctl status nebula
```

## 数据备份 / 迁移

```bash
# 备份：拷一个文件即可
cp data/memory_vectors.db backup-$(date +%F).db
# 迁移：新机器上指向同一个 db 文件即可
export NEBULA_DB_PATH=/path/to/backup.db
```

## API 速查

完整文档见 `docs/USAGE.md`（或运行后 `GET /help?level=full`）。

| 方法 | 路径 | 用途 |
|------|------|------|
| POST | `/ask` | 终极问答（ultimate 引擎） |
| POST | `/v5/answer` | 同 ultimate |
| POST/GET | `/v5/bootstrap` | 会话注入 |
| POST/GET | `/search` | 快速检索（compact） |
| POST | `/search_rerank` | 向量+改写+rerank |
| POST | `/memory/add` | 写入记忆 |
| POST | `/memory/supersede` | 标记过时 |
| GET | `/v5/health` | 健康/成熟度/trust 分布 |
| GET | `/help` | 文档（mini/short/full） |
| POST | `/mcp` | MCP JSON-RPC |

## 许可

MIT License（见 LICENSE）。
