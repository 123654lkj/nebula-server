# 星枢 Nebula 5.2.0

> Agent 不要把本 full 文档默认塞进 system prompt。默认 `GET /help`（mini）。

## 1. 产品

| 项 | 值 |
|----|-----|
| 名称 | 星枢 Nebula Memory |
| 版本 | **5.2.0** |
| Base | `http://192.168.31.252:26670`（仅局域网，不在团子 244） |
| 真理库 | Obsidian：`/home/huhu/obsidian-vault/notes/` |
| 向量 | `qwen2.5-vl-embedding` · 2048 维 · 百炼 |
| 精排 | `qwen3-rerank`（`NEBULA_RERANK=0` 关） |
| LLM | `NEBULA_LLM_MODEL`（改写 / 深搜 / 压缩，**不排序**） |
| 进程 | `nebula-memory.service` |

角色：笔记是现行正文；星枢是索引 + 门禁。冲突听笔记。

## 2. Agent 流程

```
1) POST /v5/bootstrap {"focus":"主题"}
2) POST /ask {"query":"...","top_k":5}
   → 优先 contract；其次 pack
3) trust=canon/source 且 executable → 可执行
4) 有 readback → 回读笔记原文
5) 要长期有效 → POST /memory/promote 写回 06-Agent会话提炼/
```

## 3. 端点

### 文档

| 方法 | 路径 |
|------|------|
| GET | `/help` mini |
| GET | `/help?level=short` |
| GET | `/help?level=full` |
| GET | `/help?format=json` |

### 检索

| 方法 | 路径 | 要点 |
|------|------|------|
| POST/GET | `/ask` | contract / pack / composed |
| POST | `/v5/bootstrap` | 会话注入 |
| POST/GET | `/search` | compact hybrid |
| POST | `/v4/reflect` | 仅 reflect |

```bash
curl -sS http://192.168.31.252:26670/ask \
  -H 'Content-Type: application/json' \
  -d '{"query":"GATEWAY_LOCK 网络冻结","top_k":5}'
```

| 字段 | 含义 |
|------|------|
| `contract` | 裁决（结论 / conf / executable） |
| `composed.executable` | 能否当现行 |
| `results[].trust` | canon / source / synthesis / hearsay / superseded |
| `results[].readback` | 笔记回读命令 |
| `results[].image_url` | 图片记忆回显 |

加速：`{"llm_deep":"off","llm_answer":false}`

### 写入 / 治理

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/memory/add` | 索引一条；可带 `image` |
| POST | `/memory/promote` | 写成 Obsidian 笔记 |
| GET | `/memory/image/<id>` | 回显图片 |
| PUT/DELETE | `/memory/<id>` | 改 / 删 |
| POST | `/memory/supersede` | 过时标记 |
| GET | `/v5/health` | 成熟度 / trust / vault_chunks |
| POST | `/v5/lifecycle` | 降权 |
| POST | `/v5/session-extract` | 会话抽取（线索，不是真理） |

### 密钥（Vaultwarden）

明文只进密码本。`POST /secrets/store` · `POST /secrets/get`（`reveal=true`）· `GET /secrets/status`

## 4. trust

| trust | 含义 | Agent |
|-------|------|-------|
| canon | 权威笔记 / 锁 | 可执行 |
| source | 高置信 / 04–05 笔记 | 可执行，宜核对 |
| synthesis | 汇总 | 需验证 |
| hearsay | 会话碎片 | 线索；有笔记在场不得当现行 |
| superseded | 过时 | **禁止** |

问「上次 / 会话里」才放行碎片。

## 5. 图片

`POST /memory/add`：`image` = URL / data URI / 路径 / 文件。单张 ≤ 5MB。  
说明写在 `content`（BM25）；向量只嵌图。问句带「图片/截图」会加搜 `category=image`。

## 6. 禁止

1. API key / 密码写入向量  
2. 只信 chunk、不回读 `vault:`  
3. 把 full `/help` 或 search 全文默认塞进每轮上下文  
4. 未授权改网关（`GATEWAY_LOCK`）

## 7. 运维

| 单元 | 作用 |
|------|------|
| `nebula-memory.service` | API |
| `vault-nebula-sync.timer` | 笔记 → 索引，15 min |
| `nebula-regression.timer` | 家用 24 题 |
| `nebula-lifecycle.timer` | 降权 |

回归：`python3 /opt/nebula/scripts/nebula_regression.py`  
版本只改 `/opt/nebula/nebula_meta.py` 与 `VERSION`。
