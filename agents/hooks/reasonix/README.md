# Reasonix 适配

Reasonix 桌面端有内置 memory 目录，另可通过：

1. **Skills**：若版本支持 skill 目录，拷贝 `agents/skills/*`
2. **系统提示片段**：合并 `agents/snippets/AGENTS.memory.md` 到
   - 项目 `AGENTS.md`，或
   - Reasonix `config.toml` → `[agent] system_prompt_file = "…/AGENTS.memory.md"`
3. **Session 工具约定**：在提示中要求首轮调用：
   `POST $NEBULA_BASE_URL/v5/bootstrap`
4. **可选 hook 脚本**：Reasonix 若提供 startup command，运行
   `session-start-memory.sh|ps1`

```toml
# config.toml 示例（路径按本机改）
[agent]
# system_prompt_file = "C:/path/to/nebula-open/agents/snippets/AGENTS.memory.md"
```

环境变量：`NEBULA_BASE_URL=http://127.0.0.1:26670`
