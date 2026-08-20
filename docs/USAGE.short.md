# 星枢 API 速查（short）

Base `http://192.168.31.252:26670` · **5.2.0**  
向量 `qwen2.5-vl-embedding` 2048 · 精排 `qwen3-rerank` · LLM（改写/深搜）`NEBULA_LLM_MODEL`

## Agent 必用

| 场景 | 方法 | 用返回 |
|------|------|--------|
| 开场 | POST `/v5/bootstrap` `{"focus":"任务"}` | **bootstrap** |
| 问答 | POST `/ask` `{"query":"...","top_k":5}` | **contract** > pack |
| 写入索引 | POST `/memory/add` | id, trust |
| 写回真理库 | POST `/memory/promote` `{"title","content"}` | path, vault_key |
| 图片入库 | POST `/memory/add` + `image` | image_url |
| 占用/清理 | GET `/stats` · POST `/memory/gc` | rss_mb, compacted |
| 重复记忆 | GET `/memory/dupes` | hash 分组 |
| 补分层 | POST `/memory/infer-layer` | semantic/episodic/procedural |
| 笔记同步 | POST `/vault/sync` `{force,dry_run,only,prune}` | stats, pruned |
| 同步状态 | GET `/vault/status` | root, last_run, interval |

## 裁决

- `composed.executable==true` 才当现行
- `vault:` / `notes/` 压过 session-extract
- 有 `readback` → `rxt read --host huhu …`
- `superseded` 禁止当现行

## /ask 常用

`llm_deep`: auto\|on\|off · `no_cache`: true · `image`: 以图搜

## 禁止

密钥入向量 · 只信 chunk 不回读笔记 · 把 full `/help` 塞进每轮 prompt
