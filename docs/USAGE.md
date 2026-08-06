# 星枢 Nebula 使用文档（full）

> **给 Agent 的铁律**：不要把本 full 文档默认塞进 system prompt。  
> 需要时再 `GET /help?level=short|full`。默认只用 `level=mini`（`GET /help`）。

## 1. 服务

| 项 | 值 |
|----|-----|
| Base URL | `http://<NEBULA_HOST>:<NEBULA_PORT>`（默认 `0.0.0.0:26670`） |
| 版本 | v5.0-ultimate |
| Embedding | qwen2.5-vl-embedding · 2048 维 · 百炼（`BAILIAN_API_KEY`） |
| LLM（改写/裁决/压缩） | qwen3.7-plus（`NEBULA_LLM_MODEL`） |
| Reranker（可选） | 本地 bge-reranker-v2-m3 |
| 数据 | SQLite `data/memory_vectors.db`（单文件，可备份迁移） |

## 2. Agent 标准流程

```
1) 接任务 → POST /v5/bootstrap {"focus":"主题"} → 注入 bootstrap
2) 提问   → POST /ask {"query":"...","top_k":5}
         → 优先 contract；其次 pack；勿整表 results 全文
3) trust=canon/source 且 composed.executable → 可执行
4) 有 readback → 回读原文（vault root 由 NEBULA_VAULT_ROOT 配置）
5) 重要结论 → POST /memory/add（双写自己的笔记库）
```

## 3. 端点一览

### 3.1 用法（本文档）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/help` | 默认 **mini**（~300 字，省 token） |
| GET | `/help?level=mini` | 同上 |
| GET | `/help?level=short` | 速查 ~1KB |
| GET | `/help?level=full` | 完整文档（长） |
| GET | `/help?format=json` | JSON：`{level,text,chars,est_tokens,hint}` |

### 3.2 检索 / 问答

| 方法 | 路径 | 默认 | 返回要点 |
|------|------|------|----------|
| POST/GET | `/ask` | engine=ultimate | contract, pack, composed, results(compact) |
| POST | `/v5/answer` | 同 ultimate | 同上 |
| POST/GET | `/v5/bootstrap` | — | bootstrap 会话注入 |
| POST/GET | `/search` | pack+compact+hybrid | results 短 snippet |
| POST | `/search_rerank` | — | 向量+改写+rerank |
| POST | `/v4/reflect` | — | 仅 reflect 引擎 |

#### POST /ask 示例

```bash
curl -sS http://127.0.0.1:26670/ask \
  -H 'Content-Type: application/json' \
  -d '{"query":"你的问题","top_k":5}'
```

重要字段：

| 字段 | 含义 |
|------|------|
| `contract` | 裁决短文（结论/ conf / executable）**优先用** |
| `pack` | 省 token 证据列表 |
| `composed.executable` | 是否可当现行事实执行 |
| `composed.confidence` | 0~1 |
| `results[].trust` | canon/source/synthesis/hearsay/superseded |
| `results[].readback` | 原文回读提示 |
| `token_stats.est_tokens` | 粗估 token |
| `result_cache_hit` | 缓存命中（热路径毫秒级） |

加速：

```json
{"query":"...","llm_deep":"off","llm_answer":false}
```

### 3.3 写入 / 治理

| 方法 | 路径 | body |
|------|------|------|
| POST | `/memory/add` | content, category, importance, source, tags? |
| PUT | `/memory/{id}` | content?, category?, importance? |
| DELETE | `/memory/{id}` | — |
| POST | `/memory/supersede` | old_id, new_id?, note? |
| GET | `/memory/{id}/related` | 关系邻居 |
| POST | `/v5/lifecycle` | demote_days?, demote_max? |
| POST | `/v4/migrate` | rebuild_links? |
| GET | `/v5/health` | 成熟度/trust/links |

### 3.4 其它

| 方法 | 路径 |
|------|------|
| GET | `/health` |
| GET | `/stats` |
| GET | `/ui` |
| POST | `/mcp` |

## 4. 类别 category

`ai` `code` `decision` `fact` `identity` `infrastructure` `lesson` `network` `person` `preference` `project` `security`

## 5. trust 语义

| trust | 含义 | Agent |
|-------|------|-------|
| canon | 权威（vault/现行） | 可执行 |
| source | 高置信人工/验证 | 可执行，宜核对 |
| synthesis | 汇总/一般 | 需验证 |
| hearsay | 会话碎片 | 线索 |
| superseded | 已过时 | **禁止当现行** |

## 6. 兼容旧客户端

- 旧 `/search` **不断**；默认变短（compact）。要全文：`"full":true` 或 `"pack":false`
- 旧「默认 rewrite=true」已关闭；需要：`"rewrite":true`
- 不读 `contract` 仍可读 `results[].content`（snippet）

## 7. 禁止

1. 把 API key / 密码写入向量  
2. 只信 chunk 不回读原文（`vault:` source）  
3. 把 full 文档或 search 全文默认塞进每轮上下文  
4. 未授权改生产配置

## 8. MCP

- `search_memories` / `ask_pack` → 星枢 /ask  
- `bootstrap_memories` → /v5/bootstrap  
- `remember` → 优先直写 /memory/add  

## 9. 相关文件

- 本目录：`docs/`（仓库内）
- 部署：见 README.md + install.sh
