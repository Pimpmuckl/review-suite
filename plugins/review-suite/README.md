# Review Suite

Stateful local and GitHub review workflows for Codex work.

## Install

```powershell
codex plugin marketplace add https://github.com/Pimpmuckl/review-suite --ref main
codex plugin add review-suite@review-suite
```

## Skills

- `review`: default local orchestrator.
- `review-plan`: review a written plan.
- `review-deslop`: simplify a completed slice.
- `review-state`: choose the next review action.
- `review-github`: anchored GitHub PR review.

## Review Modes

Modes are built from one phased stack. Discovery uses GPT 5.4 for high-recall bug finding; signoff uses GPT 5.5 for relevance, convergence, and current-head green checks.
Deslop passes are folded into any review that isn't using `emergency` as target; close the sidecar with `review.py --id <id> --deslop-done`.

Arena loops are only used when user config opts in with `arena.enabled` plus a nonzero `normal_arena_loops` or `deep_arena_loops` budget.
When enabled, arena loops spend discovery budget first. Each discovery phase still keeps at least one fixed GPT 5.4 pass as the safety net.

```text
|---- Emergency Phase ----
|
|--- Urgent Signoff - Single fast batch
     |- GPT 5.5 Medium x2
```

```text
|--- Brief / Normal Phase ----
|    |
|    |--- Medium Discovery - Until: normal_discovery_loops
|    |    |- Brief:  GPT 5.4 Medium x4, fixed fast pass
|    |    |- Normal: optional review_t1 arena rounds
|    |    |- Normal: GPT 5.4 Medium x4 for remaining passes, min once
|    |
|    |--- Medium Signoff - Until: Green
|    |    |- GPT 5.5 Medium x2
|
|
|--- Deep Phase ----
|    |
|    |--- Brief / Normal stack first
|    |    |- Medium Discovery
|    |    |- Medium Signoff
|    |
|    |--- Deep Discovery - Until: deep_discovery_loops
|    |    |- optional review_t3 arena rounds
|    |    |- GPT 5.4 XHigh x2 for remaining passes, min once
|    |
|    |--- Deep Signoff - Until: Green
|    |    |- GPT 5.5 XHigh x2
|
|
|--- Github Phase ----
     |--- GitHub - Until: Green/Waived
          |- findings -> fix -> follow-up -> rerun Deep Signoff
```

To explicitly replace an existing local review ladder with a stricter one, use the selected review id:

```powershell
<python> <review-suite-plugin-root>/scripts/review.py --id rvw_xxx --restart-mode deep --reason "why this run needs stricter review"
```

`--restart-mode` only allows strictness escalation (`brief` to `normal`/`deep`, or `normal` to `deep`). The old review is marked superseded, and the replacement review starts as a fresh ladder for the same repo/base/branch/head/merge-base. The worktree must be clean.

## Rules

- Start with `--mode brief|normal|deep|emergency`.
- Follow emitted `Action.cmd`.
- After an id exists, do not repeat creation flags.
- Classify reviewer output before coding valid findings.
- Review orchestration expects committed review changes. If `git diff` is non-empty but `base..HEAD` is empty, commit intended changes or stash unrelated worktree changes before rerunning.
- Run focused review-relevant validation before dispatch.
- Do not wait on slow full-suite/CI before dispatch; start it after the review round and track it as `pending`, `passed`, `failed`, or `waived/classified`.
- Do not call PR-final/merge-ready while full-suite/CI is pending or unknown.

## Commands

```powershell
<python> <review-suite-plugin-root>/scripts/review.py --mode brief|normal|deep|emergency --cd <repo-root> --base main
<python> <review-suite-plugin-root>/scripts/review.py --id <id> --decision clean|findings
<python> <review-suite-plugin-root>/scripts/review.py --id <id> --show-findings
<python> <review-suite-plugin-root>/scripts/review.py --id <id> --github-review
<python> <review-suite-plugin-root>/scripts/review.py --id <id> --github-result clean|findings
<python> <review-suite-plugin-root>/scripts/review.py --id <id> --github-result waived --github-note "why"
<python> <review-suite-plugin-root>/scripts/review.py --id <id> --restart-mode deep --reason "why this run needs stricter review"
```

`<review-suite-plugin-root>` is the installed Codex plugin cache root, such as `%USERPROFILE%\.codex\plugins\cache\review-suite\review-suite\0.1.0` for the git marketplace install or `%USERPROFILE%\.codex\plugins\cache\jonat-local\review-suite\local` for the older local marketplace install. Do not run scripts from `%USERPROFILE%\.codex\.tmp\marketplaces\review-suite`; that is Codex's marketplace source clone.

## Output

- Long runs emit `OK <minutes>m: ...` about every 60s.
- Reviewer text prints once in the final `Output:` block.
- If the calling Codex session was restarted after reviewers completed, recover the stored reviewer text with `review.py --id <id> --show-findings`; this does not launch or collect another review round.
- If GitHub review finds issues, record them on the owning review id with `--github-result findings`; after the fix follow-up is clean, that id reruns the final local signoff step before requesting GitHub again.
- If GitHub cannot run or the parent workflow approves no GitHub review, record the explicit escape hatch with `--github-result waived --github-note "why"`.

## Runtime Copies

- When launched from an installed Codex plugin cache, long-running entrypoints re-exec from `~/.codex/plugin-runtimes/review-suite/<version-hash>/`.
- Emitted `Action.cmd` may point at the runtime copy after bootstrap. Follow it; the runtime path is expected and avoids Windows locks on the installed cache.
- Review state remains under `~/.codex/state/review-suite`.
- Runtime directories are content-addressed and intentionally not aggressively cleaned up while Codex sessions may still be using them.

## Notes

- Use `python` on Windows and usually `python3` on WSL/Linux.
- If native WSL resolves `codex` to `/mnt/c/.../codex`, rerun from Windows with `--cd //wsl.localhost/<Distro>/...` and `--wsl`.
