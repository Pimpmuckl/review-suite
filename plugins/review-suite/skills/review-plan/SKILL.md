---
name: review-plan
description: "Review a written task plan before implementation / user-facing proposal."
---

# Review Plan

Use before implementation when the plan should be reviewed.

```powershell
<python> <review-suite-plugin-root>/scripts/review_plan.py --input-file <plan-path>
<python> <review-suite-plugin-root>/scripts/review_plan.py --input-text "<plan text>"
```

`<review-suite-plugin-root>` is the installed Codex plugin cache root, not `%USERPROFILE%\.codex\.tmp\marketplaces\review-suite`.

Rules:
- Prefer `--input-file`.
- Use once per meaningful plan draft.
- Verify findings before changing the plan.
