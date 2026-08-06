# Hooks · Agent 会话门禁样例

本目录提供 **可移植** 的 SessionStart 钩子：会话一开始就注入纪律口令，并尝试从 L2（星枢 / Nebula）拉一份 **有预算的 bootstrap**。

> 不是某台机器的私有脚本拷贝。路径、URL、CLI 一律用环境变量。

## 组件

| 文件 | 作用 |
|------|------|
| `session-start-memory.sh` | SessionStart 执行体（fail-open） |
| `grok-session-start.example.json` | Grok `~/.grok/hooks/*.json` 形态 |
| `claude-settings.example.json` | Claude Code `settings.json` hooks 形态（示意） |
| `cursor-hooks.example.json` | Cursor `hooks.json` 形态（示意） |

## 设计原则

1. **Fail-open**：L2 超时/不可用不阻断会话，只打印降级提示  
2. **预算**：bootstrap 默认短（`NEBULA_BOOTSTRAP_BUDGET`，如 800 字符）  
3. **无密钥**：脚本不读 `.env` 里的 API key 写入日志；只调记忆 HTTP/CLI  
4. **可替换 CLI**：优先 `NEBULA_CLI`，其次 `curl` 打 `NEBULA_BASE_URL`  
5. **与五门 skill 对齐**：hook 只做「提醒 + 小包召回」；硬门禁在 skill 行为里  

## 环境变量

| 变量 | 默认 | 含义 |
|------|------|------|
| `NEBULA_BASE_URL` | `http://127.0.0.1:26670` | L2 HTTP 根 |
| `NEBULA_BOOTSTRAP_PATH` | `/v5/bootstrap` | bootstrap 路径 |
| `NEBULA_BOOTSTRAP_BUDGET` | `800` | 字符预算 |
| `NEBULA_BOOTSTRAP_FOCUS` | `session-start` | 默认 focus |
| `NEBULA_CLI` | （空） | 若设置，优先执行：`$NEBULA_CLI bootstrap …` |
| `NEBULA_HOOK_TIMEOUT_S` | `8` | 单次召回超时秒 |
| `NEBULA_NO_PROXY` | `localhost,127.0.0.1,::1` | 可选：避免代理劫持本机 L2 |

## 安装（Grok 示例）

```bash
# 1) 拷贝脚本到你的 hooks 目录（路径自定）
install -m 755 hooks/session-start-memory.sh ~/.grok/hooks/session-start-memory.sh

# 2) 写 hook 配置（注意改成你的脚本绝对路径）
cat > ~/.grok/hooks/session-memory.json <<'JSON'
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "timeout": 12,
            "command": "bash ~/.grok/hooks/session-start-memory.sh"
          }
        ]
      }
    ]
  }
}
JSON

# 3) 可选：导出 L2 地址
# echo 'export NEBULA_BASE_URL=http://127.0.0.1:26670' >> ~/.bashrc
```

重启 Grok 会话；在 Hooks 面板确认 `SessionStart` 已加载。

## 与 skills 的关系

```text
SessionStart hook
  → 打印五门口令 + 尝试 L2 bootstrap（短）
  → 不替代 skill

编码任务
  → workflow-discipline 总门禁
  → recall-before-code / ponytail / verify-before-assert / correction-capture
```

五门 skill 样例见仓库 [`skills/`](../skills/)。

## 不要做的事

- 把生产机器绝对路径写进上游 PR  
- 在 hook 日志里打印密钥或完整记忆库 dump  
- 把 timeout 拉到几十秒阻塞会话启动  
- fail-closed 导致 L2 一挂 Agent 完全无法工作（除非你有明确的离线策略）

## OpenCode?

OpenCode does **not** load these shell SessionStart JSON hooks.
Use [`integrations/opencode/`](../integrations/opencode/) (plugin + skills + AGENTS) instead.
See [`docs/11-OpenCode适配.md`](../docs/11-OpenCode适配.md).

