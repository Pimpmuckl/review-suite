---
name: review-followup
description: "Review only the fix interdiff after a valid finding."
---

# Review Followup

Use this after fixing a valid reviewer finding when you want the narrow interdiff reviewed instead of rerunning the full diff.

```powershell
<python> <review-suite-plugin-root>/scripts/review_followup.py --base main --cd <repo-root> --note-file .review-suite/fix-note.md
<python> <review-suite-plugin-root>/scripts/review_followup.py --base main --cd <repo-root> --since <reviewed-head> --note "<compact RCA note>"
```

Use `python` on Windows and usually `python3` on WSL/Linux. If native WSL resolves `codex` to `/mnt/c/.../codex`, rerun from Windows with `--cd //wsl.localhost/<Distro>/...` and `--wsl`. Add `--cd <repo-root>` when launching outside the target repo.

Rules:
- This lane is ungraded.
- A compact root-cause note is required. Use:
  - `invariant`
  - `owner/source_of_truth`
  - `sibling_paths_checked`
  - `structural_fix`
  - `regression_coverage`
- If `--since` is omitted, the wrapper uses the latest recorded review anchor for the current branch.
- This lane reviews only `<last_reviewed_head>..HEAD`.
- The wrapper enforces a small-delta guard. If the delta has grown too large, stop and use `review-state status`.
- Even if the latest delta is small, the wrapper may still reject another narrow follow-up when the branch has already accumulated too much review churn.
- More than two follow-up rounds since the last graded/full checkpoint should usually be treated as "split or reset", not another normal follow-up.
- Launch follow-up after focused, review-relevant validation for the fix is green; full-suite/CI can continue as merge-readiness validation.
- Use `--force` only when you intentionally want to bypass that guard.
- Relative `--note-file` paths resolve against `--cd <repo-root>`, not the launcher cwd.
