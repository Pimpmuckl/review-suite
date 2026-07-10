---
name: review
description: "Run local code review/status; choose `fast`, `brief`, `normal`, or `deep` by risk."
---

# Review

Use for local code review.

```powershell
<python> <review-suite-plugin-root>/scripts/review.py --mode fast|brief|normal|deep --cd <repo-root> --base main
```

Path rules:
- `<review-suite-plugin-root>` is the installed Codex plugin cache root, such as `%USERPROFILE%\.codex\plugins\cache\review-suite\review-suite\0.1.0` for the git marketplace install.
- Do not run scripts from `%USERPROFILE%\.codex\.tmp\marketplaces\review-suite`; that is Codex's marketplace source clone.
- Follow emitted `Action.cmd`; runtime-backed runs should emit installed-launcher paths for follow-up commands.

Mode:
- `fast`: small, localized, well-tested changes; GPT 5.6 Sol medium signoff only, no deslop, and no more than two local review rounds.
- `brief`: one four-model medium discovery brawl, then GPT 5.6 Sol medium signoff.
- `normal`: default behavior changes; four-model medium discovery budget, then GPT 5.6 Sol medium signoff.
- `deep`: stateful, cross-system, security, concurrency, migration, and other high-risk work; normal stack, four-model xhigh discovery budget, GPT 5.6 Sol xhigh signoff, then GitHub when required.

Rules:
- Run focused validation before dispatch; start slow full-suite/CI after dispatch and track final status.
- Non-fast runs may include a deslop sidecar. When emitted and handled/no longer useful, close it only with `review.py --id <id> --deslop-done`.
- To replace an existing ladder with stricter review, use `review.py --id <id> --restart-mode deep --reason "<why>"` while the original repo/base/branch/head/merge-base still match and the worktree is clean; plain `--mode deep --cd <repo-root>` is not a restart.
- Review orchestration expects committed review changes. If `git diff` is non-empty but `base..HEAD` is empty, commit intended changes or stash unrelated worktree changes before rerunning.
- Review commands do not support a dirty-worktree override. Do not append `--allow-dirty`; commit intended review changes first.
- Read `Output:`, then follow the emitted `Action.cmd`.
- For arena grading actions, grade only after checking findings against the diff/repo. Plausible but unverified findings do not count as valid. Use `scope_bloat_loss` when a review asks for product behavior, AI guardrails, validation, fallback behavior, UX policy, or safety checks that are not required by the diff, a real bug, a trust boundary, or the user request.
- Supply the requested rating pool and repeat `--rank` from best to worst; comma-separated variants within one rank tie.
- Without an id, use `review.py --status --cd <repo-root> --base main` for branch/gate routing.
- The normal advance command after a review id exists is bare `review.py --id <id>`; explicit `--decision clean|findings` is an override when Review Suite cannot auto-advance from a structured reviewer verdict or a human intentionally disagrees.
- For a read-only id check, run `review.py --id <id> --show-status`.
- If the caller session was restarted after reviewer output was produced, run `review.py --id <id> --show-findings` to recover stored reviewer text without launching another review.
- Classify reviewer output before coding valid findings.
- Fix valid findings, then run emitted `review.py --id <id>`.
- After GitHub review returns, record the result on the owning review id: `--github-result clean`, `--github-result findings`, or `--github-result waived --github-note "why"`. Do not start a new ladder for GitHub findings.
- After an id exists, do not repeat creation flags.
- Do not call PR-final/merge-ready until full-suite/CI is passed or waived/classified.
