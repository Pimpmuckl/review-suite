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
- Run emitted `action.cmd` when present.
- Do not inspect raw state JSON.
- Use after head changes once a branch has reached T2/T3/T4.
- Do not step down lanes unless this command routes there.
- If it emits `needs-grade`, grade before starting another local review lane.
- If it emits `fix-gate-findings`, inspect the stored gate output, fix valid findings, then rerun this command.
