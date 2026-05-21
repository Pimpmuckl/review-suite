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

- `brief`: tiny same-seam fix, docs/wording, focused regression proves it.
- `normal`: normal backend/runtime seam with no durable lifecycle or data-ownership change.
- `deep`: leases, retries, queues, terminal state, schema/data ownership, or security.
- `emergency`: blocked stack/local run; fix now and soak after.

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
<python> <review-suite-plugin-root>/scripts/review_github.py run --cd <repo-root>
```

`<review-suite-plugin-root>` is the installed Codex plugin cache root, such as `%USERPROFILE%\.codex\plugins\cache\review-suite\review-suite\0.1.0` for the git marketplace install or `%USERPROFILE%\.codex\plugins\cache\jonat-local\review-suite\local` for the older local marketplace install. Do not run scripts from `%USERPROFILE%\.codex\.tmp\marketplaces\review-suite`; that is Codex's marketplace source clone.

## Output

- Long runs emit `OK <minutes>m: ...` about every 60s.
- Reviewer text prints once in the final `Output:` block.

## Runtime Copies

- When launched from an installed Codex plugin cache, long-running entrypoints re-exec from `~/.codex/plugin-runtimes/review-suite/<version-hash>/`.
- Emitted `Action.cmd` may point at the runtime copy after bootstrap. Follow it; the runtime path is expected and avoids Windows locks on the installed cache.
- Review state remains under `~/.codex/state/review-suite`.
- Runtime directories are content-addressed and intentionally not aggressively cleaned up while Codex sessions may still be using them.

## Notes

- Use `python` on Windows and usually `python3` on WSL/Linux.
- If native WSL resolves `codex` to `/mnt/c/.../codex`, rerun from Windows with `--cd //wsl.localhost/<Distro>/...` and `--wsl`.
