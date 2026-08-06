# MCP 适配（通用）

星枢服务在安装可选依赖 `mcp` 后可暴露 MCP 工具；也可用 HTTP 封装：

| 工具语义 | HTTP |
|----------|------|
| bootstrap | `POST /v5/bootstrap` |
| ask | `POST /ask` |
| remember | `POST /memory/add` |
| health | `GET /v5/health` |

任意支持 MCP/HTTP 工具的 Agent（Claude、Cursor、自定义）按上表注册即可。

环境变量：`NEBULA_BASE_URL`
