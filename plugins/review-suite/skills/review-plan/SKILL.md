---
name: review-plan
description: "Review a written task plan before implementation."
---

# Review Plan

Use this before implementation to stress-test a written plan.

```powershell
<python> <review-suite-plugin-root>/scripts/review_plan.py --input-file <plan-path>
<python> <review-suite-plugin-root>/scripts/review_plan.py --input-text "<plan text>"
```

Use `python` on Windows and usually `python3` on WSL/Linux. If native WSL resolves `codex` to `/mnt/c/.../codex`, rerun from Windows with `--cd //wsl.localhost/<Distro>/...` and `--wsl`. Use `--input-file` by default. Use `--input-text` only when the plan exists inline.

Rules:
- This lane is for structure and scope, not code correctness review.
- Treat findings as input and verify locally before changing the plan.
- Run it once per plan draft, not repeatedly after each small wording change.
