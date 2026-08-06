# 11 · OpenCode 适配

> 依据 [anomalyco/opencode](https://github.com/anomalyco/opencode) 源码与官方文档整理。  
> 落点样例：[`integrations/opencode/`](../integrations/opencode/)。

## 1. 为什么不能照搬 Grok hook

| Grok | OpenCode |
|------|----------|
| `~/.grok/hooks/*.json` + shell `SessionStart` | **无**对等 shell SessionStart 配置模型 |
| Skills 多在 `~/.grok/skills` | Skills 扫 `.opencode/skills`、`~/.config/opencode/skills`、`.claude`/`.agents` |
| 规则 / AGENTS | `AGENTS.md` / `CLAUDE.md`（`session/instruction.ts`） |
| — | **Plugin Hooks**（`packages/plugin`）：事件、工具、改 system prompt |

适配原则：**同一套 L0–L5 纪律与 SKILL.md，换运行时落点。**

## 2. OpenCode 扩展点（源码地图）

### 2.1 Skills

- 发现：`packages/opencode/src/skill/index.ts` → `discoverSkills`
- 模式：
  - 配置目录：`{skill,skills}/**/SKILL.md`
  - 外部：`~/.claude|~/.agents` 与项目上溯 `skills/**/SKILL.md`
  - `config.skills.paths` / `skills.urls`
- 注入：`session/system.ts` 把 skill 列表写进 system；**全文靠 `skill` 工具按需加载**
- 约束：frontmatter `name`+`description` 必填；**目录名 = name**；name 正则 `^[a-z0-9]+(-[a-z0-9]+)*$`

→ 本仓库 `skills/` 可直接拷到 OpenCode skills 目录。

### 2.2 Instructions

- `session/instruction.ts`：全局 `~/.config/opencode/AGENTS.md` + 项目首个 `AGENTS.md`/`CLAUDE.md`
- 适合：**短**记忆纪律（见 `AGENTS.snippet.md`），不要塞 full 手册

### 2.3 Plugins

- 类型：`packages/plugin` → `Plugin` 返回 `Hooks`
- 自动加载：`~/.config/opencode/plugins/*`、`.opencode/plugins/*`
- 与 Nebula 最贴的 hook：
  - **`experimental.chat.system.transform`**：改 `output.system[]` → 会话级 bootstrap（对齐 Grok SessionStart）
  - **`tool`**：注册 `nebula_ask` / `nebula_add` / `nebula_bootstrap`
  - 可选：`tool.execute.before` 做危险命令策略（另案）

### 2.4 配置路径

- 全局：`~/.config/opencode/opencode.json(c)`
- 项目：`opencode.json` + 上溯 `.opencode/`
- Schema：`https://opencode.ai/config.json`

### 2.5 MCP

- `packages/opencode/src/mcp/*`：可把 L2 当 MCP server 挂入
- 与 plugin tools **二选一或并存**；纪律仍靠 skills + AGENTS

## 3. 推荐装配（最小闭环）

```text
OpenCode 启动
  → 加载 plugins/nebula-memory.js
  → 发现 skills（五门 + nebula-recall）
  → 读 AGENTS.md 片段
  → 首次 chat：system.transform 注入预算 bootstrap（fail-open）
  → 编码任务：skill 召回 / nebula_ask
  → 纠正：nebula_add + correction-capture
```

## 4. 与六层记忆的对应

| 层 | OpenCode 侧 |
|----|-------------|
| L0 | 会话消息窗口 |
| L1 | 仓库 `AGENTS.md` / 笔记（readback） |
| L2 | Nebula HTTP 或 MCP + plugin tools |
| L3 | 外部 session archive（OpenCode 不管） |
| L4 | 外部 secrets；禁止写入 L2 |
| L5 | OpenCode 自带 bash/edit/grep… 实测 |

## 5. 反模式

| 反模式 | 原因 |
|--------|------|
| 只装 skill 不装 bootstrap | 冷启动仍失忆 |
| system 注入 full 文档 | 违背 OpenCode 低 overhead + 我们的预算原则 |
| skill 目录名 ≠ name | OpenCode 校验失败 |
| 把 Grok hooks JSON 丢进 `.opencode` | 不会被执行 |
| 插件里写死内网主机表 | 不可移植、有泄露风险 |

## 6. 文件索引

| 路径 | 说明 |
|------|------|
| `integrations/opencode/README.md` | 安装与验证 |
| `integrations/opencode/plugins/nebula-memory.js` | 插件实现 |
| `integrations/opencode/opencode.json.example` | 配置样例 |
| `integrations/opencode/AGENTS.snippet.md` | AGENTS 片段 |
| `skills/*` | 共用五门 + nebula-recall |

## 7. 验收

- [ ] 新会话 system 中有 bootstrap 或 fail-open 提示  
- [ ] `skill` 可见六门技能  
- [ ] `nebula_ask` 打到你的 L2 返回 compact JSON  
- [ ] 样例无私人拓扑与密钥  
