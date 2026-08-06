# 星枢 Nebula 速查（short）

> Base: `http://<host>:<port>` · version `v5.0-ultimate` · 默认端口 26670

## 铁律
- `/help` 默认 mini，勿把 full 塞进 system prompt
- 提问用 `/ask`，优先读 `contract` → `pack` → `results`
- `trust=canon/source` 且 executable → 可执行；`superseded` 禁用
- 有 `readback` → 回读原文
- API key/密码禁止写入向量

## 端点
| 路径 | 用途 |
|------|------|
| POST `/ask {"query","top_k"}` | 终极问答（ultimate） |
| POST `/v5/bootstrap {"focus"}` | 会话注入 |
| POST `/memory/add` | 写入 |
| POST `/memory/supersede` | 标记过时 |
| GET `/v5/health` | 健康/成熟度 |
| GET `/help?level=short|full` | 本文档 |

## 加速
```json
{"query":"...","llm_deep":"off","llm_answer":false}
```

## category
`ai` `code` `decision` `fact` `identity` `infrastructure` `lesson` `network` `person` `preference` `project` `security`
