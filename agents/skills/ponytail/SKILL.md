---
name: ponytail
description: >
  Lazy ladder: solve with least code. config > one-liner > stdlib > installed dep
  > minimal diff > full build. Triggers: yagni, simplify, shortest path.
compatibility: opencode
---

# Ponytail（懒惰阶梯）

Lazy = efficient, not sloppy. **Best code is code not written.**

## Ladder (stop at first yes)

1. Need it at all?  
2. Stdlib?  
3. OS/native tools?  
4. Already-installed dependency?  
5. One-liner?  
6. Minimal working diff  

## Hard rules

- Prefer delete over add; no “for later” scaffolding  
- No single-impl interface/factory theater  
- Safety / validation / data loss prevention: **not lazy**  

## Output

Ship the change first. At most three lines: what you skipped and when to add it.
