---
name: correction-capture
description: >
  On user correction, close the loop: fix now, distill rule, write back (supersede if needed).
compatibility: opencode
---

# Correction Capture（纠正闭环）

Apology without writeback = same mistake next session.

## Three steps (fixed order)

1. **Fix the immediate issue**  
2. **Distill one rule** — scene | wrong | right | (optional) user phrase  
3. **Write back** (at least one place, prefer L2)  
   - `memory/add` category=`lesson` importance≥0.6  
   - or `supersede` when it contradicts a current item  
   - optional: append one line to the relevant L1 note  

## Hard fail

- Session ends after a clear correction with **zero writeback**  
- New current guidance contradicts old without **supersede**  

## Extra on bugs

Search callers / shared sites; guard once — don’t patch a single call site only.
