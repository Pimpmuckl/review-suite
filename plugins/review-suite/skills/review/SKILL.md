---
name: review
description: "Run the default local review orchestrator."
---

# Review

Use for local review unless the user asks for a specialist lane.

```powershell
<python> <review-suite-plugin-root>/scripts/review.py --mode brief|normal|deep|emergency --cd <repo-root> --base main
```

Modes: `brief`, `normal`, `deep`, `emergency`.

Rules:
- Non-emergency runs deslop once.
- Read `Output:`, then run one emitted `Action.cmd`.
- For local review decisions, use only `--id <id>` and `--decision clean|findings` when asked.
- After an id exists, do not repeat `--mode`, `--cd`, or `--base`.
- Classify reviewer output before coding valid findings.
- Fix valid findings, then run emitted `review.py --id <id>`.
- When HEAD changed, `review.py --id <id>` runs follow-up.
- Clean follow-up resumes profile progression.
- Final clean reaches `review-green`.
- After `review-green`, run the emitted GitHub `Action.cmd`.
- Run focused validation before dispatch; start slow full-suite/CI after dispatch and track final status.
