---
name: nebula-recall
description: >
  Call L2 semantic memory (Nebula-style) before coding or asserting history.
  Use with SessionStart hook bootstrap; ask for lessons; contract fields only;
  honor executable/trust/readback; supersede corrections.
compatibility: opencode
---

# Nebula Recall

## Pair with hooks

SessionStart (`hooks/session-start-memory.sh`) or OpenCode plugin (`integrations/opencode/plugins/nebula-memory.js`) injects a **small** bootstrap.  
This skill governs **ongoing** recalls during the session.

## When

- Task start → ensure bootstrap ran (hook or manual)  
- Code / bug / infra / “last time” → `ask`  
- User correction → `supersede` + lesson  

## Hard rules

1. No recall before edit on historical/infra tasks = violation  
2. Eat **contract / bootstrap** only  
3. `executable=false` / `superseded` → not current  
4. `readback` → re-open L1  
5. Secrets never enter L2  

## Flow

```text
hook bootstrap → ask → adjudicate → L1/L5 verify → act → add|supersede
```
