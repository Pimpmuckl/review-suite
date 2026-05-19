---
name: review-t4
description: "Run local PR signoff after `review-t3` is green."
---

# Review T4

Use after green `review-t3`.

```powershell
<python> <review-suite-plugin-root>/scripts/review_t4.py --base main --cd <repo-root>
```

Use `python` on Windows and usually `python3` on WSL/Linux. Add `--cd <repo-root>` when launching outside the target repo.

Rules:
- Signoff lane. Do not grade.
- Completion leaves the gate in `signoff_pending`.
- Read the final `Output:` block for reviewer text.
- Close with emitted `Action.cmd`; replace `VERDICT` with `clean` or `findings`.
- Do not step down to T3 after T4. Use `review-state`.
- Code only valid findings; verify locally and add regression coverage.
- Launch after focused review-relevant validation is green.
- Run slow full-suite/CI after dispatch and resolve relevant failures before PR-final/merge-ready.
- Move on only after both reviewers are effectively green and the gate is closed `clean`.
