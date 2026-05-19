# Review Suite

Local and GitHub review lanes for Codex work.

## Install

```powershell
codex plugin marketplace add https://github.com/Pimpmuckl/review-suite --ref main
codex plugin add review-suite@review-suite
```

## Lanes

- `review`: default local orchestrator.
- `review-plan`: review a written plan.
- `review-deslop`: simplify a completed slice.
- `review-state`: choose the next review action.
- `review-followup`: review a fix interdiff.
- `review-t1`: graded slice review.
- `review-t2`: slice signoff.
- `review-t3`: graded PR-ready review.
- `review-t4`: PR signoff.
- `review-github`: anchored GitHub PR review.

## Rules

- Use `review.py` unless the user asks for a specialist lane.
- Start with `--mode brief|normal|deep|emergency`.
- Non-emergency runs deslop once, then one review profile step per invocation.
- For local review decisions, follow emitted `Action.cmd` with only `--id <id>` and `--decision clean|findings`.
- After an id exists, do not repeat `--mode`, `--cd`, or `--base`.
- Classify reviewer output before coding valid findings.
- Fix valid findings, then run emitted `review.py --id <id>`.
- When HEAD changed, `review.py --id <id>` runs follow-up.
- Clean follow-up resumes profile progression.
- Final clean reaches `review-green`.
- After `review-green`, run the emitted GitHub `Action.cmd`.
- Use specialist lanes only when the user asks for them.
- Run focused review-relevant validation before dispatch.
- Do not wait on slow full-suite/CI before dispatch; start it after the review round and track it as `pending`, `passed`, `failed`, or `waived/classified`.
- Do not call PR-final/merge-ready while full-suite/CI is pending or unknown; investigate and fix relevant failures first.
- Read the final `Output:` block, then follow the final `Action.cmd`.

## Commands

```powershell
<python> <review-suite-plugin-root>/scripts/review.py --mode brief|normal|deep|emergency --cd <repo-root> --base main
<python> <review-suite-plugin-root>/scripts/review.py --id <id> --decision clean|findings
<python> <review-suite-plugin-root>/scripts/review_github.py run --cd <repo-root>
```

## Output

- Long runs emit `OK <minutes>m: ...` about every 60s.
- Reviewer text prints once in the final `Output:` block.
- Follow the final `Action.cmd`; replace placeholders such as `VERDICT`, `WINNER`, and `BASIS`.

## Notes

- Use `python` on Windows and usually `python3` on WSL/Linux.
- If native WSL resolves `codex` to `/mnt/c/.../codex`, rerun from Windows with `--cd //wsl.localhost/<Distro>/...` and `--wsl`.
- Keep temporary review notes in `.review-suite/`.
