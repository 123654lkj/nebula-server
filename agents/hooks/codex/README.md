# Codex 适配

Codex 主要靠 **skills + AGENTS.md**（无 Grok 式 SessionStart JSON）。

1. Skills → `~/.codex/skills/<name>/SKILL.md`
2. 合并 `agents/snippets/AGENTS.memory.md` 到全局或项目 `AGENTS.md`
3. 可选：在项目 `AGENTS.md` 写死 `NEBULA_BASE_URL` 使用约定

推荐会话首条用户消息前由 skill `workflow-discipline` + `nebula-recall` 触发 bootstrap。
