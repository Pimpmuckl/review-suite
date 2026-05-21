---
name: review-state
description: "Inspect local review progress or the next required review action for the current repo."
---

# Review State

Use to check review progress or choose the next review action.

```powershell
<python> <review-suite-plugin-root>/scripts/review_state.py status --base main --cd <repo-root>
```

Path rules:
- `<review-suite-plugin-root>` is the installed Codex plugin cache root, not `%USERPROFILE%\.codex\.tmp\marketplaces\review-suite`.
- Follow emitted `Action.cmd` when it points at `%USERPROFILE%\.codex\plugin-runtimes\review-suite\<version-hash>\scripts\...`; that runtime path is expected after bootstrap.

Rules:
- Deterministic compact TOON.
- Run emitted `Action.cmd` when present.
- Use `--verbose` only when compact output lacks enough routing detail.
