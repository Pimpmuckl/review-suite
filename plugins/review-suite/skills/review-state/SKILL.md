---
name: review-state
description: "Inspect the last review anchor and recommend the next lane."
---

# Review State

Use this when you need to know what was last reviewed on the current branch and whether the next step should be arena grading, waiting for an in-flight round, follow-up, coherence/reset, or a fresh full review.

```powershell
<python> <review-suite-plugin-root>/scripts/review_state.py status --base main --cd <repo-root>
```

Use `python` on Windows and usually `python3` on WSL/Linux. This command does not launch Codex, so `--wsl` is not needed. For WSL-hosted repos, run from native WSL with the Linux repo path; do not use a Windows UNC `//wsl.localhost/...` path for `review-state`. Add `--cd <repo-root>` when launching outside the target repo.

Rules:
- This command is deterministic and emits compact TOON.
- Do not inspect review-suite state JSON directly; this command is the public state surface.
- It does not decide whether a branch is clean.
- Current recommendation buckets are `needs-grade`, `wait-round`, `signoff-decision`, `fix-gate-findings`, `review-followup`, `coherence-review`, `full-review`, and `none`.
- Caller-local arena rounds take precedence over narrower follow-up/full-review suggestions.
- `review-t2` and `review-t4` should treat this as the branch-signoff preflight source of truth.
- When this emits `action.cmd`, run that command instead of inferring a lane.
- Review lanes are monotonic for a branch. If this emits `recommended_lane`, run that lane instead of stepping down to T1/T2/T3 from an already higher stage.
- After a head change on a branch that already reached T2/T3/T4, this tool decides the next lane. Do not require lower-tier green status on the final head.
- After clean `review-t4`, local review is green for that exact head/base. Do not go back to `review-t3` unless `review-state` explicitly says so.
- T2/T4 findings are not clean anchors. If this emits `fix-gate-findings`, inspect the stored gate output, fix valid findings, then rerun `review-state`; after a clean follow-up it will route back to the same gate tier.
- Even if the latest interdiff is small, this tool may still escalate to `coherence-review` when the branch has already accumulated too much review churn.
- More than two follow-up rounds since the last graded/full checkpoint should be treated as a split-or-reset warning, not normal narrow follow-up.
- If the current head is already reviewed and the only dirty files sit outside the committed branch diff, this stays signoff-clean instead of forcing `review-followup`.
- If there is no `action.cmd`, do not infer `review-t1` versus `review-t3` from this tool alone.
