---
name: review
description: "Run local code review/status; choose `fast`, `normal`, or `deep` by risk."
---

# Review

Use for local code review.

```powershell
<python> <review-suite-plugin-root>/scripts/review.py --cd <repo-root>
```

Path rules:
- `<review-suite-plugin-root>` is the installed Codex plugin cache root, such as `%USERPROFILE%\.codex\plugins\cache\review-suite\review-suite\0.1.0` for the git marketplace install.
- When reviewing Review Suite itself, use the current source checkout only if the user explicitly requests dogfooding unsynced source changes.
- Do not run scripts from `%USERPROFILE%\.codex\.tmp\marketplaces\review-suite`; that is Codex's marketplace source clone.
- Follow emitted `Action.cmd`; runtime-backed runs should emit installed-launcher paths for follow-up commands.

Mode:
- Omit `--mode` for `normal`, the default for ordinary changes.
- Use `--mode fast` for UI-only, local presentation, and other small, well-tested changes. It runs dual GPT-5.6 Sol medium signoff with no deslop, Arena, or GitHub review and stops after at most two local rounds.
- Use `--mode deep` for billing, login/authentication, authorization/security, business-critical systems, database integrity or migrations, concurrency, and similarly critical or high-blast-radius logic.
- Treat those mappings as risk heuristics. A nominally UI-only change that crosses a trust or data-integrity boundary is not `fast`.

Rules:
- Run focused validation before dispatch; start slow full-suite/CI after dispatch and track final status.
- Normal and deep run deslop as a sidecar; handle or dismiss its output before completion, then close it with `review.py --id <id> --deslop-done`.
- To replace an existing ladder with stricter review, use `review.py --id <id> --restart-mode deep --reason "<why>"` while the original repo/base/branch/head/merge-base still match and the worktree is clean; plain `--mode deep --cd <repo-root>` is not a restart.
- When the local round budget is exhausted, follow the emitted `review.py --id <id> --new-cycle`; it starts one same-mode successor and rejects non-exhausted reviews.
- Review orchestration expects committed review changes. If `git diff` is non-empty but `base..HEAD` is empty, commit intended changes or stash unrelated worktree changes before rerunning.
- Review commands do not support a dirty-worktree override. Do not append `--allow-dirty`; commit intended review changes first.
- Read `Output:`, then follow the emitted `Action.cmd`.
- On `head_changed_after_review`, inspect `reviewed_head..current_head`. If the changes only fix stale tests to match already-reviewed behavior, do not rerun review; run the affected tests and required validation, then proceed. Rerun only if production code or intended behavior changed.
- For arena grading actions, grade only after checking findings against the diff/repo. Plausible but unverified findings do not count as valid. Use `scope_bloat_loss` when a review asks for product behavior, AI guardrails, validation, fallback behavior, UX policy, or safety checks that are not required by the diff, a real bug, a trust boundary, or the user request.
- Supply the requested rating pool and repeat `--rank` from best to worst; comma-separated variants within one rank tie. The caller grades; Review Suite never promotes a winner automatically.
- Without an id, use `review.py --status --cd <repo-root>` for branch/gate routing.
- The default base is the repository's remote default branch. Use `--base <ref>` only as an explicit override.
- The normal advance command after a review id exists is bare `review.py --id <id>`; explicit `--decision clean|findings` is an override when Review Suite cannot auto-advance from a structured reviewer verdict or a human intentionally disagrees.
- For a read-only id check, run `review.py --id <id> --show-status`.
- If the caller session was restarted after reviewer output was produced, run `review.py --id <id> --show-findings` to recover stored reviewer text without launching another review.
- Classify reviewer output before coding valid findings.
- Fix valid findings, then run emitted `review.py --id <id>`.
- After GitHub review returns, record the result on the owning review id: `--github-result clean`, `--github-result findings`, or `--github-result waived --github-note "why"`. Do not start a new ladder for GitHub findings.
- After an id exists, do not repeat creation flags.
- Do not call PR-final/merge-ready until full-suite/CI is passed or waived/classified.
