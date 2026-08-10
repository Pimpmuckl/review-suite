---
name: review-plan
description: "Review a written task plan before implementation / user-facing proposal."
---

# Review Plan

Use before implementation when the plan should be reviewed.

```powershell
<python> <review-suite-plugin-root>/scripts/review_plan.py --input-file <plan-path>
<python> <review-suite-plugin-root>/scripts/review_plan.py --input-text "<plan text>"
<python> <review-suite-plugin-root>/scripts/review_plan.py
Get-Content <plan-path> | <python> <review-suite-plugin-root>/scripts/review_plan.py
```

Use the installed Codex plugin cache root by default. When reviewing Review Suite itself, use the current source checkout only if the user explicitly requests dogfooding unsynced source changes. Never use `%USERPROFILE%\.codex\.tmp\marketplaces\review-suite`.

Rules:
- Prefer `--input-file`.
- With no input flags, the script reads `task_plan.md` from the current directory; stdin is accepted for pipes.
- Use once per meaningful plan draft.
- Inspect the repository to identify root cause, canonical ownership, and relevant invariants before recommending a shape.
- Compare only materially distinct credible solution shapes. Accept one obvious solution instead of inventing alternatives.
- Rank contract correctness, canonical ownership, durable concepts/coupling, and justified blast radius/validation ahead of diff size; use diff size only as a tie-breaker.
- Return exactly one `PROCEED`, `REVISE`, or `RETHINK` verdict with concise evidence and actionable changes. `RETHINK` recommends reconsideration; it does not grant scope authority.
- Verify findings before changing the plan.
