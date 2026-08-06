# Agent 接入层（完整记忆系统的另一半）

服务 + vault 只解决 **存与同步**。Agent 要真的用记忆，还需要：

| 层 | 是什么 | 目录 |
|----|--------|------|
| **Skills** | 行为门禁（五门 + nebula-recall） | `agents/skills/` |
| **Hooks** | 会话启动 bootstrap（fail-open） | `agents/hooks/` |
| **Snippets** | 可合并的 AGENTS 指令 | `agents/snippets/` |
| **Integrations** | OpenCode 插件 / MCP 示例 | `agents/integrations/` |
| **Installer** | 一键装到各 Agent | `install-agents.sh` / `.ps1` |

## 支持的 Agent 矩阵

| Agent | Skills | SessionStart Hook | 指令片段 | 备注 |
|-------|--------|-------------------|----------|------|
| **Grok** | `~/.grok/skills` | `hooks/session-memory.json` | AGENTS.md | bash 或 pwsh |
| **Codex** | `~/.codex/skills` | （靠 skill 触发） | AGENTS.md | |
| **OpenCode** | `~/.config/opencode/skills` | 插件 system.transform | AGENTS.snippet | 见 integrations/opencode |
| **Claude Code** | `~/.claude/skills` | settings hooks | CLAUDE/AGENTS | |
| **Cursor** | skills 目录 | hooks.json | rules/AGENTS | |
| **Hermes** | `~/.hermes/skills` | 可选脚本 | AGENTS.nebula.md | |
| **Reasonix** | 模板 skills | system_prompt_file | AGENTS.memory.md | 见 hooks/reasonix |
| **OpenClaw** | workspace/skills | 可选 | 工作区 AGENTS.md | `OPENCLAW_WORKSPACE` |

## 一键安装

```bash
# Linux / macOS / WSL
export NEBULA_BASE_URL=http://127.0.0.1:26670
./agents/install-agents.sh all

# 只装一部分
./agents/install-agents.sh grok codex hermes opencode
```

```powershell
# Windows
$env:NEBULA_BASE_URL = "http://127.0.0.1:26670"
pwsh -File .gents\install-agents.ps1 -Targets grok,codex,reasonix
```

或：

```bash
npm run agents:install
```

## 五门 + 召回 skill

| Skill | 作用 |
|-------|------|
| workflow-discipline | 总门禁与接任务顺序 |
| ponytail | 最少代码阶梯 |
| recall-before-code | 改前召回 L2 |
| verify-before-assert | 断言前验证 |
| correction-capture | 纠正写回 |
| nebula-recall | L2 字段吃法（contract/readback） |

## 架构

```text
SessionStart hook ──fail-open──► bootstrap(短预算)
         │
         ▼
   用户任务
         │
         ├─ workflow-discipline
         ├─ recall-before-code ──► POST /ask|/v5/bootstrap
         ├─ ponytail
         ├─ verify-before-assert
         └─ correction-capture ──► POST /memory/add|supersede
                    │
                    ▼
         L1 vault 原文 ◄── readback
```

## 不要做的事

- 把私人主机清单 / 密钥写进 hook  
- fail-closed 导致 L2 挂了 Agent 完全不能工作  
- 只装 server 不装 skills（等于记忆系统「有库无纪律」）
