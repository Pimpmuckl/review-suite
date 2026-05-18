# Review Suite

Local and GitHub review lanes for Codex work.

## Lanes

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

- Use the narrowest lane.
- Use `review-state status` after T2/T3/T4 or when unsure.
- Code only valid findings.
- Track full-suite/CI as `pending`, `passed`, `failed`, or `waived/classified`.
- Completion lines are status-only.
- Current runs print reviewer text in the final `Output:` block.
- Use `show-round` / `show-last` only to revisit stored output.
- Do not inspect raw state JSON or rollout logs.
- T2/T4 gates must be closed as `clean` or `findings`.

## Commands

```powershell
<python> <review-suite-plugin-root>/scripts/review_state.py status --base main --cd <repo-root>
<python> <review-suite-plugin-root>/scripts/review_t1.py --base main --cd <repo-root>
<python> <review-suite-plugin-root>/scripts/review_t2.py --base main --cd <repo-root>
<python> <review-suite-plugin-root>/scripts/review_t3.py --base main --cd <repo-root>
<python> <review-suite-plugin-root>/scripts/review_t4.py --base main --cd <repo-root>
<python> <review-suite-plugin-root>/scripts/review_github.py run --cd <repo-root>
```

## Output

- Long runs emit `OK <minutes>m: ...` about every 60s.
- T1-T4 reviewer completion lines show wrapper status only.
- Reviewer text prints once in the final `Output:` block.
- Follow the final `Action.cmd`; replace placeholders such as `VERDICT`, `WINNER`, and `BASIS`.
- Stored reviewer text is available through:

```powershell
<python> <review-suite-plugin-root>/scripts/review_suite_arena.py show-round --round-id <id>
<python> <review-suite-plugin-root>/scripts/review_suite_arena.py show-last --cd <repo-root>
```

## Notes

- Use `python` on Windows and usually `python3` on WSL/Linux.
- If native WSL resolves `codex` to `/mnt/c/.../codex`, rerun from Windows with `--cd //wsl.localhost/<Distro>/...` and `--wsl`.
- Keep temporary review notes in `.review-suite/`.
