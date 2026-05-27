---
name: review
description: "Run local code review after focused validation; choose `brief`, `normal`, `deep`, or `emergency` by risk."
---

# Review

Use for local code review.

```powershell
<python> <review-suite-plugin-root>/scripts/review.py --mode brief|normal|deep|emergency --cd <repo-root> --base main
```

Path rules:
- `<review-suite-plugin-root>` is the installed Codex plugin cache root, such as `%USERPROFILE%\.codex\plugins\cache\review-suite\review-suite\0.1.0` for the git marketplace install.
- Do not run scripts from `%USERPROFILE%\.codex\.tmp\marketplaces\review-suite`; that is Codex's marketplace source clone.
- Follow emitted `Action.cmd` even when it points at `%USERPROFILE%\.codex\plugin-runtimes\review-suite\<version-hash>\scripts\...`; that runtime path is expected after bootstrap.

Mode:
- `brief`: tiny same-seam fix, docs/wording, focused regression proves it.
- `normal`: normal backend/runtime seam with no durable lifecycle or data-ownership change.
- `deep`: leases, retries, queues, terminal state, schema/data ownership, or security.
- `emergency`: blocked stack/local run; fix now and soak after.

Rules:
- Run focused validation before dispatch; start slow full-suite/CI after dispatch and track final status.
- To replace an existing ladder with stricter review, use `review.py --id <id> --restart-mode deep --reason "<why>"` while the original repo/base/branch/head/merge-base still match and the worktree is clean; plain `--mode deep --cd <repo-root>` is not a restart.
- Review orchestration expects committed review changes. If `git diff` is non-empty but `base..HEAD` is empty, commit intended changes or stash unrelated worktree changes before rerunning.
- Read `Output:`, then follow the emitted `Action.cmd`.
- If the caller session was restarted after reviewer output was produced, run `review.py --id <id> --show-findings` to recover stored reviewer text without launching another review.
- Classify reviewer output before coding valid findings.
- Fix valid findings, then run emitted `review.py --id <id>`.
- After GitHub review returns, record the result on the owning review id: `--github-result clean`, `--github-result findings`, or `--github-result waived --github-note "why"`. Do not start a new ladder for GitHub findings.
- After an id exists, do not repeat creation flags.
- Do not call PR-final/merge-ready until full-suite/CI is passed or waived/classified.
