---
name: review-t2
description: "Run local slice signoff after `review-t1` is green."
---

# Review T2

Use after green `review-t1`.

```powershell
<python> <review-suite-plugin-root>/scripts/review_t2.py --base main --cd <repo-root>
<python> <review-suite-plugin-root>/scripts/review_t2.py --commit <sha> --cd <repo-root>
<python> <review-suite-plugin-root>/scripts/review_t2.py --commit <from-sha> <to-sha> --cd <repo-root>
```

Use `python` on Windows and usually `python3` on WSL/Linux. Add `--cd <repo-root>` when launching outside the target repo. Prefer `--commit` for a committed slice.

Rules:
- Signoff lane. Do not grade.
- Completion leaves the gate in `signoff_pending`.
- Read the final `Output:` block for reviewer text.
- Close with emitted `Action.cmd`; replace `VERDICT` with `clean` or `findings`.
- Do not inspect raw state JSON or rollout logs.
- Do not rerun T1 after T2 has run; use `review-state`.
- Code only valid findings. Verify locally and check adjacent code.
- Add regression tests for bugs found.
- Launch after focused review-relevant validation is green; track full-suite/CI separately.
- Move on only after both reviewers are effectively green and the gate is closed `clean`.
