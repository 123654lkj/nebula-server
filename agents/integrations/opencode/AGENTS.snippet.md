<!--
  Portable fragment: merge into ~/.config/opencode/AGENTS.md
  or project AGENTS.md. No host inventory. No secrets.
-->

## Memory stack (L0–L5) — Nebula L2

- **L0** session window · **L1** authority notes · **L2** Nebula semantic memory
- **L3** session archive · **L4** secrets store · **L5** code/runtime truth

### Hard gates

1. Engineering / infra / “last time” → call **nebula_bootstrap** / **nebula_ask** (or `skill` recall-before-code) **before** edits.
2. Consume **contract / bootstrap** only — never dump raw search hits.
3. `executable=false` or `superseded` → not current policy.
4. `readback` present → re-open L1 authority text.
5. Secrets → L4 only; never write keys into L2.
6. User correction → fix now, then **nebula_add** / supersede (correction-capture).

### OpenCode wiring

- Skills: `workflow-discipline`, `ponytail`, `recall-before-code`, `verify-before-assert`, `correction-capture`, `nebula-recall`
- Plugin tools: `nebula_bootstrap`, `nebula_ask`, `nebula_add`
- Session inject: plugin `experimental.chat.system.transform` (budgeted, fail-open)

### Lazy ladder

config → one-liner → stdlib → installed dep → minimal diff → full build.
