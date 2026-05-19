---
name: review-suite
description: "Route review work to the right review-suite lane."
---

# Review Suite

Use when the review lane is not already explicit.

Route:
- `review`: default local orchestrator.
- `review-plan`: plan review before implementation.
- `review-deslop`: cleanup pass after implementation.
- `review-state`: next review action / pending grade / in-flight round.
- `review-followup`: narrow review after fixing a valid finding.
- `review-t1`: graded slice review.
- `review-t2`: slice signoff after green T1.
- `review-t3`: graded PR-ready review after green T2.
- `review-t4`: PR signoff after green T3.
- `review-github`: standalone anchored GitHub PR review.

Rules:
- Use `review.py` unless the user asks for a specialist lane.
- Start with `--mode brief|normal|deep|emergency`.
- Non-emergency runs deslop once, then one review profile step per invocation.
- Follow emitted `Action.cmd` with only `--id <id>` and `--decision clean|findings` when asked.
- Classify reviewer output before coding valid findings.
- Fix valid findings, then run emitted `review.py --id <id>`.
- When HEAD changed, `review.py --id <id>` runs follow-up.
- Clean follow-up resumes profile progression.
- Final clean reaches `review-green`.
- GitHub review is standalone.
- T1/T2/T3/T4 are expert/debug/benchmark escape hatches.
- Keep one review lane active unless the user explicitly asks otherwise.
- Follow `review-state` after T2/T3/T4; do not step down after amended commits.
- T2/T4 gates must be closed as `clean` or `findings`; only `clean` records the workflow anchor.
- Completion lines are status-only. Current runs print reviewer text in the final `Output:` block.
- Follow the final `Action.cmd`.
- Use `show-round` / `show-last` only to revisit stored output.
- Do not inspect raw review-suite state JSON or rollout logs.
- Code only valid findings. Escalate unclear product decisions or conflicts with explicit direction.
- Run focused review-relevant validation before dispatch.
- Do not wait on slow full-suite/CI before dispatch; start it after the review round and track it as `pending`, `passed`, `failed`, or `waived/classified`.
- Do not call PR-final/merge-ready while full-suite/CI is pending or unknown; investigate and fix relevant failures first.
- Do not tune model, reasoning, polling, progress, or timeout settings.
