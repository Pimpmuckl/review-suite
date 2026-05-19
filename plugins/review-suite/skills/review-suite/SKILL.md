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
- `review-t1` / `review-t2` / `review-t3` / `review-t4`: specialist local lanes.
- `review-github`: standalone anchored GitHub PR review.

Rules:
- Use `review.py` unless the user asks for a specialist lane.
- Start with `--mode brief|normal|deep|emergency`.
- Non-emergency runs deslop once, then one review profile step per invocation.
- For local review decisions, follow emitted `Action.cmd` with only `--id <id>` and `--decision clean|findings`.
- Classify reviewer output before coding valid findings.
- Fix valid findings, then run emitted `review.py --id <id>`.
- When HEAD changed, `review.py --id <id>` runs follow-up.
- Clean follow-up resumes profile progression.
- Final clean reaches `review-green`.
- After `review-green`, run the emitted GitHub `Action.cmd`.
- Use specialist lanes only when the user asks for them.
- Read `Output:`, then follow the final `Action.cmd`.
- Code only valid findings. Escalate unclear product decisions or conflicts with explicit direction.
- Run focused review-relevant validation before dispatch.
- Do not wait on slow full-suite/CI before dispatch; start it after the review round and track it as `pending`, `passed`, `failed`, or `waived/classified`.
- Do not call PR-final/merge-ready while full-suite/CI is pending or unknown; investigate and fix relevant failures first.
- Do not tune model, reasoning, polling, progress, or timeout settings.
