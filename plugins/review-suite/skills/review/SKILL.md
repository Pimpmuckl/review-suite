---
name: review
description: "Run local code review after focused validation; choose `brief`, `normal`, `deep`, or `emergency` by risk."
---

# Review

Use for local code review.

```powershell
<python> <review-suite-plugin-root>/scripts/review.py --mode brief|normal|deep|emergency --cd <repo-root> --base main
```

Mode:
- `brief`: tiny same-seam fix, docs/wording, focused regression proves it.
- `normal`: normal backend/runtime seam with no durable lifecycle or data-ownership change.
- `deep`: leases, retries, queues, terminal state, schema/data ownership, or security.
- `emergency`: blocked stack/local run; fix now and soak after.

Rules:
- Run focused validation before dispatch; start slow full-suite/CI after dispatch and track final status.
- Read `Output:`, then follow the emitted `Action.cmd`.
- Classify reviewer output before coding valid findings.
- Fix valid findings, then run emitted `review.py --id <id>`.
- After an id exists, do not repeat creation flags.
- Do not call PR-final/merge-ready until full-suite/CI is passed or waived/classified.
