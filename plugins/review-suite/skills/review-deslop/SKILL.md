---
name: review-deslop
description: "Simplify a completed implementation slice before or between review passes."
---

# Review Deslop

Use this after an implementation slice when you want one cleanup pass before correctness review, or when `review-state` points you at a coherence/reset pass.

```powershell
<python> <review-suite-plugin-root>/scripts/review_deslop.py --base main --cd <repo-root>
<python> <review-suite-plugin-root>/scripts/review_deslop.py --commit <sha> --cd <repo-root>
<python> <review-suite-plugin-root>/scripts/review_deslop.py --commit <from-sha> <to-sha> --cd <repo-root>
```

Use `python` on Windows and usually `python3` on WSL/Linux. If native WSL resolves `codex` to `/mnt/c/.../codex`, rerun from Windows with `--cd //wsl.localhost/<Distro>/...` and `--wsl`. Add `--cd <repo-root>` when launching outside the target repo.

Rules:
- This lane is for simplification, not arena or gate review.
- Verify findings locally before editing.
- After a valid finding, check adjacent code in the same subsystem and clean it up together.
- Use `--focus` when you want a coherence/reset pass after repeated reviewer-driven fixes on the same subsystem.
- Do not loop this until green. If it devolves into noise, move on.
