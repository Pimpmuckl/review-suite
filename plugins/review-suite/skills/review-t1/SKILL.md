---
name: review-t1
description: "Run the graded local slice-review lane."
---

# Review T1

Use after an implementation slice and before local signoff.

```powershell
<python> <review-suite-plugin-root>/scripts/review_t1.py --base main --cd <repo-root>
<python> <review-suite-plugin-root>/scripts/review_t1.py --commit <sha> --cd <repo-root>
<python> <review-suite-plugin-root>/scripts/review_t1.py --commit <from-sha> <to-sha> --cd <repo-root>
```

Use `python` on Windows and usually `python3` on WSL/Linux. Add `--cd <repo-root>` when launching outside the target repo. Prefer `--commit` for a committed slice.

Rules:
- Graded lane. Grade with emitted `Action.cmd`.
- Read the final `Output:` block for reviewer text.
- Do not use after the branch reaches T2/T3/T4. Run `review-state` and follow its action.
- Code only valid findings; verify locally and add regression coverage.
- Use `review-followup` after fixing valid findings unless `review-state` routes wider.
- Launch after focused review-relevant validation is green.
- Run slow full-suite/CI after dispatch and resolve relevant failures before PR-final/merge-ready.
- Use tie only when reviews are materially indistinguishable.
