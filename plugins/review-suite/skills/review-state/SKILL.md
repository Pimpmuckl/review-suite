---
name: review-state
description: "Inspect the last review anchor and recommend the next lane."
---

# Review State

Use to choose the next review action.

```powershell
<python> <review-suite-plugin-root>/scripts/review_state.py status --base main --cd <repo-root>
```

Rules:
- Deterministic compact TOON.
- Run emitted `Action.cmd` when present.
- Do not inspect raw state JSON.
- Use after head changes once a branch has reached T2/T3/T4.
- Do not step down lanes unless this command routes there.
- If it emits `needs-grade`, grade before starting another local review lane.
- If it emits `fix-gate-findings`, run `Action.show_cmd`, fix valid findings, then rerun this command.
- Use `--verbose` only when compact output lacks enough routing detail.
