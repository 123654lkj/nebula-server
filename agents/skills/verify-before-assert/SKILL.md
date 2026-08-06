---
name: verify-before-assert
description: >
  Verify before asserting IP/port/path/version/history. If verification fails, say uncertain.
compatibility: opencode
---

# Verify Before Assert（验证再断言）

Confident hallucination costs more than honest ignorance.

## Must verify

- Host / port / path / version  
- “It was X” / “should be Y”  
- Counts, timings, sizes  
- Secrets → **L4 only**, never echo full secrets into chat logs if policy forbids  

## Ladder

1. L2 ask / contract  
2. Read file / pack  
3. Run command / test / status  
4. Still none → **uncertain** + what is missing  

## Output

Evidence first when you have it; never fabricate when you don’t.
