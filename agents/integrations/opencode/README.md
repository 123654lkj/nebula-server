# OpenCode integration (Nebula L2)

How to wire **星枢 / Nebula** into [OpenCode](https://github.com/anomalyco/opencode) using its **real extension points** (skills, plugins, AGENTS.md, MCP)—not Grok-style shell SessionStart hooks.

Source basis (OpenCode `dev` branch):

| Mechanism | Where in OpenCode | Nebula use |
|-----------|-------------------|------------|
| Skills | `packages/opencode/src/skill/*` — scan `.opencode/skills`, `~/.config/opencode/skills`, `.claude/skills`, `.agents/skills` | Ship five gates + `nebula-recall` as `SKILL.md` |
| System skill list | `session/system.ts` → `skill` tool | Agent loads skill on demand |
| Instructions | `session/instruction.ts` — `AGENTS.md` / `CLAUDE.md` | Memory discipline fragment |
| Plugins | `packages/plugin` Hooks API | Bootstrap inject + tools |
| Plugin dirs | `~/.config/opencode/plugins/`, `.opencode/plugins/` | Drop-in `nebula-memory.js` |
| Config | `opencode.json` / `opencode.jsonc` | `plugin`, `skills.paths`, permissions |
| MCP | `packages/opencode/src/mcp/*` | Optional: expose L2 as MCP instead of plugin tools |

OpenCode does **not** use Grok’s `~/.grok/hooks/*.json` SessionStart. The equivalent is:

1. **Plugin** `experimental.chat.system.transform` (budgeted bootstrap, once per session)  
2. **Skills** loaded via native `skill` tool  
3. **AGENTS.md** for always-on short discipline  

---

## Quick install (global)

```bash
# from this repo root
REPO="$(pwd)"

mkdir -p ~/.config/opencode/plugins
mkdir -p ~/.config/opencode/skills

# plugin
cp integrations/opencode/plugins/nebula-memory.js ~/.config/opencode/plugins/

# skills (OpenCode requires: dir name == frontmatter name)
for s in workflow-discipline ponytail recall-before-code verify-before-assert correction-capture nebula-recall; do
  mkdir -p ~/.config/opencode/skills/$s
  cp "skills/$s/SKILL.md" ~/.config/opencode/skills/$s/SKILL.md
done

# optional AGENTS fragment (merge manually)
# cat integrations/opencode/AGENTS.snippet.md >> ~/.config/opencode/AGENTS.md

# optional: point skills.paths at the git checkout instead of copying
# see opencode.json.example
```

Set L2 endpoint:

```bash
export NEBULA_BASE_URL=http://127.0.0.1:26670   # your server
```

Restart OpenCode.

---

## Project-local install

```text
your-project/
  .opencode/
    plugins/nebula-memory.js
    skills/<name>/SKILL.md
  AGENTS.md          # merge AGENTS.snippet.md
  opencode.json      # optional plugin/skills config
```

Or in `opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "plugin": ["file:///absolute/path/to/nebula-memory.js"],
  "skills": { "paths": ["/absolute/path/to/nebula-memory/skills"] }
}
```

(Relative plugin paths resolve from the config file location.)

---

## Skill name rules (OpenCode)

From OpenCode docs / loader:

- Directory name **must** match YAML `name`
- `name`: `^[a-z0-9]+(-[a-z0-9]+)*$` (1–64 chars)
- `description` **required** (1–1024 chars)

This repo’s `skills/*` already follow that.

---

## Plugin behavior

File: [`plugins/nebula-memory.js`](plugins/nebula-memory.js)

| Hook / tool | Behavior |
|-------------|----------|
| `experimental.chat.system.transform` | Once per `sessionID`, POST bootstrap, push short system block; **fail-open** |
| `nebula_bootstrap` | Manual budgeted bootstrap |
| `nebula_ask` | POST `/ask` → compact JSON string |
| `nebula_add` | POST `/memory/add` |

Disable pieces:

```bash
export NEBULA_INJECT_SYSTEM=0
export NEBULA_REGISTER_TOOLS=0
```

---

## MCP alternative

If you already expose L2 via MCP (SSE/stdio), add it in OpenCode MCP config instead of plugin tools. Keep skills + AGENTS either way—tools alone do not enforce recall discipline.

---

## Mapping vs Grok hooks

| Grok | OpenCode |
|------|----------|
| `SessionStart` shell hook | Plugin `experimental.chat.system.transform` or first-turn `nebula_bootstrap` |
| `~/.grok/skills/*/SKILL.md` | `~/.config/opencode/skills/*/SKILL.md` (same SKILL.md) |
| AGENTS.md / rules | `AGENTS.md` via Instruction loader |
| Shell-only fail-open | Plugin catch + continue |

---

## Verification checklist

- [ ] OpenCode lists the six skills (`skill` tool / debug)  
- [ ] First message in a new session shows L2 bootstrap block or fail-open notice  
- [ ] `nebula_ask` returns contract-like JSON against your L2  
- [ ] No secrets or private host inventory committed in config samples  
