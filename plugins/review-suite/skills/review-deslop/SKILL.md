---
name: review-deslop
description: "Simplify a completed implementation slice before or between review passes."
---

# Review Deslop

Use for one cleanup pass after implementation.

```powershell
<python> <review-suite-plugin-root>/scripts/review_deslop.py --base main --cd <repo-root>
<python> <review-suite-plugin-root>/scripts/review_deslop.py --commit <sha> --cd <repo-root>
<python> <review-suite-plugin-root>/scripts/review_deslop.py --commit <from-sha> <to-sha> --cd <repo-root>
```

Rules:
- Simplification lane, not correctness signoff.
- Verify findings locally before editing.
- Use `--focus` for a coherence/reset cleanup pass.
- Do not loop this lane until green.
