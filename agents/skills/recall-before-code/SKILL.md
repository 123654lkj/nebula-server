---
name: recall-before-code
description: >
  Before code/bug/infra: recall L2 semantic memory → L1 authority → code graph.
  Triggers: edit, fix, config, “last time”, architecture choice.
compatibility: opencode
---

# Recall Before Code（召回先于改代码）

Most “quick fixes” spawn a second bug within 48h. Search ~30s first.

## Hard fail (stop editing)

1. Edit without any bootstrap/ask/mem call  
2. User said “last time” but you did not search  
3. Treat `executable=false` as current policy  
4. Skip `readback` on authority hits  
5. Claim “it used to be X” with no evidence  

On fail, emit: `[RECALL-GATE] blocked: <reason>` then recall.

## Order (token-saving)

```text
L2 bootstrap|ask  →  L1 authority readback  →  L5 pack/refs/tests  →  (optional) L3 session archive
```

Consume **contract / bootstrap** only — no raw hit dumps.

## After recall

Only then: propose plan, patch, or assert prior history.  
Zero hits → say “searched, no hit” — do not invent.
