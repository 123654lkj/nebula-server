# 星枢 API 速查

```
GET  /v5/health
GET  /help
POST /ask              {"query","top_k"}
POST /v5/bootstrap     {"focus","budget"}
POST /memory/add       {"content","category","importance"}
POST /v5/session-extract
```

Agent SOP：bootstrap → ask(contract) → 有 readback 必回读 → 重要结论写回。
