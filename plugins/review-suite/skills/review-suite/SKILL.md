---
name: review-suite
description: "Route review work to the right review-suite lane."
---

# Review Suite

Use when the review lane is not already explicit.

Route:
- `review-plan`: plan review before implementation.
- `review-deslop`: cleanup pass after implementation.
- `review-state`: next review action / pending grade / in-flight round.
- `review-followup`: narrow review after fixing a valid finding.
- `review-t1`: graded slice review.
- `review-t2`: slice signoff after green T1.
- `review-t3`: graded PR-ready review after green T2.
- `review-t4`: PR signoff after green T3.
- `review-github`: anchored GitHub PR review after local review.

Rules:
- Use the narrowest specialist skill.
- Keep one review lane active unless the user explicitly asks otherwise.
- Follow `review-state` after T2/T3/T4; do not step down after amended commits.
- T2/T4 gates must be closed as `clean` or `findings`; only `clean` records the workflow anchor.
- Completion lines are status-only. Use `show-round` / `show-last` for reviewer text.
- Do not inspect raw review-suite state JSON or rollout logs.
- Code only valid findings. Escalate unclear product decisions or conflicts with explicit direction.
- Launch review after focused, review-relevant validation is green. Track full-suite/CI separately as `pending`, `passed`, `failed`, or `waived/classified`.
- Do not tune model, reasoning, polling, progress, or timeout settings.
