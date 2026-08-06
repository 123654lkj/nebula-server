# 验证记录（nebula-open）

日期：2026-08-06

## 环境
- 隔离目录：`/tmp/nebula-verify-*`（从本仓库 rsync，不含生产数据）
- 依赖：仅 `requirements.txt` 核心三项（flask/numpy/requests），未装 torch/mcp
- 密钥：仅通过环境变量 `BAILIAN_API_KEY` 注入（不进仓库）

## 结果
| 检查项 | 结果 |
|--------|------|
| `/v5/health` | 通过 `version=v5.0-ultimate`，空库可启动 |
| `/help` | 通过 通用文档 |
| `POST /memory/add` | 通过 `status=ok`，返回 id |
| `POST /ask` | 通过 200，能召回刚写入内容 |
| secrets 无 Vault | 通过 降级返回 count=0，不崩 |
| MCP 可选 | 通过 日志：mcp 库未安装，跳过 MCP 工具注册 |

## 结论
**E2E_PASS** — 脱敏开源包可在全新环境独立运行（需有效 embedding API key）。