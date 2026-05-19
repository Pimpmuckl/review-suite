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
- Run one emitted `Action.cmd` at a time.
- Use only `--id <id>` and `--decision clean|findings` when asked.
- Classify reviewer output before coding valid findings.
- Fix valid findings, then run emitted `review.py --id <id>`.
- When HEAD changed, `review.py --id <id>` runs follow-up.
- Clean follow-up resumes profile progression.
- Final clean reaches `review-green`.
- GitHub review is standalone; use `review-github`.
- Use T1/T2/T3/T4 only as expert/debug/benchmark escape hatches.
