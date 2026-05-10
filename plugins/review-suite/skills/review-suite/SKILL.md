---
name: review-suite
description: "Route review work to the right review-suite lane."
---

# Review Suite

Use this as the umbrella entrypoint for review work when the right lane is not already explicit.

Route quickly:

- `review-plan`
  Use before implementation for plan review.

- `review-deslop`
  Use after implementation to simplify the change.

- `review-state`
  Use to inspect the latest review anchor, or to see whether a caller-local arena round still needs grading first.

- `review-followup`
  Use after fixing a valid finding when only the interdiff needs review.

- `review-t1`
  Use during active development for graded slice review.

- `review-t2`
  Use after `review-t1` is green for local signoff. If the branch already reached T2, do not rerun T1 after amended commits.

- `review-t3`
  Use once the branch is PR-ready for graded local PR review.

- `review-t4`
  Use after `review-t3` is green for final local signoff.

- `review-github`
  Use once local review is solid and the PR is ready for GitHub review.

- Cost ledger
  Use `review_suite_arena.py costs --cd <repo>` for a manual repo refresh, or `--all` for an explicit full backfill. Wrapper-triggered automatic refresh is disabled until it is append-only and cannot call GitHub.

Rules:
- Route to the narrowest specialist skill as soon as intent is clear.
- Keep one review lane active at a time unless the user explicitly asks otherwise.
- Standard flow: `review-plan` -> implement -> `review-deslop` -> `review-t1` -> valid-finding fixes through `review-followup` or `review-state` -> `review-t2` -> `review-state` -> `review-t3` -> `review-t4` -> `review-github`.
- After T2/T4 reviewer completion, inspect the outputs and close the gate as `clean` or `findings`; only a `clean` close records the workflow anchor.
- Review lanes are monotonic for a branch. Once the branch reaches T2/T3/T4, do not step down after amended commits; run `review-state` and then its emitted current-stage action instead.
- Do not invent a "T1 and T2 green on the final head" requirement after T2 has already run. The requirement is current-stage green: follow `review-state`, and rerun T2/T3/T4 as routed.
- After clean-closed `review-t4`, do not run `review-t3` again. Use `review-state` only if the branch/base changes or an external signal reopens the PR, and run its emitted action.
- If a graded local round just finished and you are unsure whether to rerun or grade it, use `review-state` before starting another arena lane.
- If a shell closes after a local round finishes, do not rerun blindly; use `review_suite_arena.py show-last --cd <repo> --state-dir <state-dir>` or `review_suite_arena.py show-round --round-id <id> --state-dir <state-dir>` to recover stored findings. For T2/T4, then run `close-gate --verdict clean` or `close-gate --verdict findings`.
- Only `review-t1` and `review-t3` are graded.
- Cost reporting currently covers local T1-T4 reviewer sessions. Treat GitHub review and nonlocal model usage as out of scope unless a later command records that telemetry explicitly.
- Before coding from reviewer output, classify each item as: valid finding, non-finding suggestion/product preference, or unclear product decision. Code only valid findings. If reviewer advice conflicts with explicit user/product direction, pause and escalate the tradeoff to the user or parent agent instead of implementing it.
- Treat valid findings as symptoms and prefer structural fixes.
- Some deep local reviews can take up to 20 minutes; wait for wrapper output and do not inspect review-suite state JSON.
- `review-t1` through `review-t4` support append-only steering via `--instructions` or `--instructions-file`.
- On base-review lanes, adding custom instructions switches the wrapper to manual merge-base diff review, which is more token-heavy than native `--base`.
- Do not tune model, reasoning, polling, progress, or timeout settings. The wrappers own those defaults.
