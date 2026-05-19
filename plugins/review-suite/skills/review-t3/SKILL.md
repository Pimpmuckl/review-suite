---
name: review-t3
description: "Run the graded local PR-ready review lane."
---

# Review T3

Use when the branch is PR-ready and T2 is green.

```powershell
<python> <review-suite-plugin-root>/scripts/review_t3.py --base main --cd <repo-root>
```

Use `python` on Windows and usually `python3` on WSL/Linux. Add `--cd <repo-root>` when launching outside the target repo.

Rules:
- Graded lane. Grade with emitted `Action.cmd`.
- Read the final `Output:` block for reviewer text.
- Do not use after the branch reaches T4. Run `review-state` and follow its action.
- Code only valid findings; verify locally and add regression coverage.
- Use `review-followup` after fixing valid findings unless `review-state` routes wider.
- Launch after focused review-relevant validation is green.
- Run slow full-suite/CI after dispatch and resolve relevant failures before PR-final/merge-ready.
- Move on only when both reviewers are effectively green on the same head.
