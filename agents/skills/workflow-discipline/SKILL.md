---
name: workflow-discipline
description: >
  Session gate: memory-first, lazy ladder, FOA≤4, verify, correction writeback.
  Pair with SessionStart hook. Triggers: any engineering task after session start.
compatibility: opencode
---

# Workflow Discipline（工作流总门禁）

Align with SessionStart hook: **bootstrap | ponytail | recall | verify | correction**

## Hard gates

| Phase | Rule |
|-------|------|
| Intake | Engineering / infra / bugfix → **bootstrap or ask first** |
| Adjudicate | `executable=false` / `superseded` → not current policy |
| Authority | Hit with `readback` → must re-open L1 text |
| Secrets | L4 secrets only; never plain into notes/vectors |
| Close | User correction → **correction-capture**; important decisions → L2 add |

## Order

1. **Recall** → `recall-before-code`  
2. **Ladder** → `ponytail`  
3. **Act** → smallest diff; large tree → one pack/map pass  
4. **Verify** → `verify-before-assert`  
5. **Writeback** → `correction-capture` / memory add when needed  

## FOA

Simultaneous focus **≤ 4**. Split or defer the rest.

## Tool discipline

| Scene | Do | Don't |
|-------|-----|--------|
| Enter repo | One structured pack/digest with budget | N× list + N× full read |
| Facts | Verify then assert | “Should be…” |
| Memory quality | Lifecycle / scorecard periodically | Only accumulate rows |

## Self-check

- [ ] Change verified  
- [ ] No unsolicited abstraction  
- [ ] Lesson/decision written if needed  
- [ ] No secrets in files/vectors  
