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
- `emergency`: urgent GPT 5.5 medium signoff only; deslop disabled; no more than two local review rounds.
- `brief`: one GPT 5.4 medium discovery pass, then GPT 5.5 medium signoff.
- `normal`: GPT 5.4 medium discovery budget, then GPT 5.5 medium signoff.
- `deep`: normal stack, GPT 5.4 xhigh discovery budget, GPT 5.5 xhigh signoff, then GitHub when required.

Rules:
- Run focused validation before dispatch; start slow full-suite/CI after dispatch and track final status.
- Non-emergency runs may include a deslop sidecar. When emitted and handled/no longer useful, close it only with `review.py --id <id> --deslop-done`.
- To replace an existing ladder with stricter review, use `review.py --id <id> --restart-mode deep --reason "<why>"` while the original repo/base/branch/head/merge-base still match and the worktree is clean; plain `--mode deep --cd <repo-root>` is not a restart.
- Review orchestration expects committed review changes. If `git diff` is non-empty but `base..HEAD` is empty, commit intended changes or stash unrelated worktree changes before rerunning.
- Review commands do not support a dirty-worktree override. Do not append `--allow-dirty`; commit intended review changes first.
- Read `Output:`, then follow the emitted `Action.cmd`.
- If the caller session was restarted after reviewer output was produced, run `review.py --id <id> --show-findings` to recover stored reviewer text without launching another review.
- Classify reviewer output before coding valid findings.
- Fix valid findings, then run emitted `review.py --id <id>`.
- After GitHub review returns, record the result on the owning review id: `--github-result clean`, `--github-result findings`, or `--github-result waived --github-note "why"`. Do not start a new ladder for GitHub findings.
- After an id exists, do not repeat creation flags.
- Do not call PR-final/merge-ready until full-suite/CI is passed or waived/classified.
