# Grok 适配

## Skills
安装到 `~/.grok/skills/<name>/SKILL.md`（或 Windows `%USERPROFILE%\.grok\skills`）

## SessionStart Hook
- Linux/macOS: `bash` + `session-start-memory.sh`
- Windows: `pwsh` + `session-start-memory.ps1`

```bash
# 由 agents/install-agents.sh 自动写入
# 手动: 拷贝 hooks/common/session-start-*.{sh,ps1}
# 配置 ~/.grok/hooks/session-memory.json
```

环境变量：`NEBULA_BASE_URL`（默认 http://127.0.0.1:26670）
