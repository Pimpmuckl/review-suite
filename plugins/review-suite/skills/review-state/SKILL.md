---
name: review-state
description: "Inspect legacy branch/gate routing when no `rvw_*` review id exists."
---

# Review State

Use only when no `rvw_*` id exists and branch/gate routing is needed. For an existing id, use `review.py --id <id> --show-status`.

```powershell
<python> <review-suite-plugin-root>/scripts/review_state.py status --base main --cd <repo-root>
```

Path rules:
- `<review-suite-plugin-root>` is the installed Codex plugin cache root, not `%USERPROFILE%\.codex\.tmp\marketplaces\review-suite`.
- Follow emitted `Action.cmd` when it points at `%USERPROFILE%\.codex\plugin-runtimes\review-suite\<version-hash>\scripts\...`; that runtime path is expected after bootstrap.

Rules:
- Branch/gate routing only; do not use for active `rvw_*` cycles.
- Deterministic compact TOON.
- Run emitted `Action.cmd` when present.
- Use `--verbose` only when compact output lacks enough routing detail.
