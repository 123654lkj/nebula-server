# OpenClaw 适配

OpenClaw 工作区通常有 `AGENTS.md` + `skills/`：

```bash
# 工作区示例
$OPENCLAW_WORKSPACE/
  AGENTS.md          # 合并 snippets/AGENTS.memory.md
  skills/            # 拷贝 agents/skills/*
```

定时学习任务若写回记忆，统一走：
- `POST /memory/add` 或 `/v5/session-extract`
- 禁止密钥入向量

Session 启动可在 agent 配置里挂：
`bash ~/.nebula/hooks/session-start-memory.sh`
