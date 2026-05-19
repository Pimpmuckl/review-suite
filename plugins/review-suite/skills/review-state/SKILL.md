---
name: review-state
description: "Inspect local review progress or the next required review action for the current repo."
---

# Review State

Use to check review progress or choose the next review action.

```powershell
<python> <review-suite-plugin-root>/scripts/review_state.py status --base main --cd <repo-root>
```

Rules:
- Deterministic compact TOON.
- Run emitted `Action.cmd` when present.
- Use `--verbose` only when compact output lacks enough routing detail.
