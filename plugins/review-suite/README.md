# Review Suite

`review-suite` is one plugin with one umbrella router and nine specialist lanes:

- `review-suite`: pick the right lane
- `review-plan`: review a written plan before implementation
- `review-deslop`: simplify a completed slice
- `review-state`: inspect the last recorded review anchor and next step
- `review-followup`: review only the fix interdiff after a valid finding
- `review-t1`: graded local slice review
- `review-t2`: local signoff after `review-t1`
- `review-t3`: graded local PR-ready review
- `review-t4`: local signoff after `review-t3`
- `review-github`: anchored `@codex review` cycle on the PR

## Core Rules

- Findings are symptoms, not patch instructions.
- Before coding from reviewer output, classify each item as: valid finding, non-finding suggestion/product preference, or unclear product decision. Code only valid findings. If reviewer advice conflicts with explicit user/product direction, pause and escalate the tradeoff to the user or parent agent instead of implementing it.
- After a valid substantive finding, write a compact root-cause note with:
  - `invariant`
  - `owner/source_of_truth`
  - `sibling_paths_checked`
  - `structural_fix`
  - `regression_coverage`
- Prefer one structural fix over stacked local guards.
- Use `review-followup` while the fix delta stays small.
- Use `review-state status` when you need to decide between follow-up, coherence/reset, or a fresh full review.
- `review-t2` and `review-t4` are branch signoff lanes. Reviewer completion leaves the gate in `signoff_pending`; the calling agent must inspect the outputs and close the round as `clean` or `findings`.
- If a signoff gate is closed as `clean`, the workflow anchor is recorded. If it is closed as `findings`, no workflow anchor is recorded.
- If the branch moved past the last valid review anchor, `review-state` may route to `review-followup` or to the same signoff lane as a fresh full-diff pass for the current stage.
- After a clean-closed `review-t4`, local review is green for that exact head/base. Proceed to `review-github`/CI/merge readiness unless the branch changes, base drift is risky, or an external signal reopens the PR.
- `review-state` can also escalate a small latest delta back to coherence/full-diff review when the branch has already accumulated too many commits and review cycles.
- As a practical rule, more than two follow-up rounds after the last graded/full checkpoint should be treated as a split-or-reset signal, not "one more tiny follow-up".
- If a completed graded round for this caller is still ungraded, `review-state status` should point you at arena grading before another local review lane.
- Some deep local reviews can take up to 20 minutes; wait for wrapper output instead of inspecting state files or rerunning.
- If a shell closes after a local round finishes, use `review_suite_arena.py show-last --cd <repo> --state-dir <state-dir>` or `review_suite_arena.py show-round --round-id <id> --state-dir <state-dir>` to recover stored findings. For T2/T4, then use `review_suite_arena.py close-gate --round-id <id> --verdict clean|findings --state-dir <state-dir>`. Do not rerun just to see output.
- Only `review-t1` and `review-t3` are graded.
- Manual review mode keeps the findings-only contract but mirrors current Codex review pressure where relevant: return every supported finding, include line numbers when available, and flag change-size, integration-surface, coverage, or agent-context risks only when they affect correctness. UX preferences, product-scope speculation, backwards-compat speculation, and alternative product direction are non-findings unless they expose a concrete risk against stated requirements, docs, code invariants, or explicit contracts.

## Default Flow

1. `review-plan`
2. implement
3. `review-deslop`
4. `review-t1`
5. valid-finding fixes -> `review-followup` or `review-state`
6. `review-t2`
7. `review-state`
8. `review-t3`
9. `review-t4`
10. `review-github`

## Custom Instructions

- `review-t1` through `review-t4` accept `--instructions "<text>"` or `--instructions-file <path>`.
- This is append-only steering. The built-in findings-only contract stays first.
- On base-review lanes, custom instructions switch the wrapper to manual merge-base diff review, which uses more tokens than native `--base`.

## CLI Surface

- Normal lanes expose target, repo, steering, dirty-worktree, and WSL controls only.
- `review-t2` and `review-t4` also accept `--champion-override <variant-id>` for temporary operator-forced signoff reviewer selection.
- Stored local review output recovery lives on `review_suite_arena.py show-last` and `review_suite_arena.py show-round`. `show-last` prints the latest stored T1-T4 outputs for a repo; `show-round` prints one exact round by id.
- T2/T4 signoff closure lives on `review_suite_arena.py close-gate --round-id <id> --verdict clean|findings`. Only `clean` writes the workflow anchor.
- Review cost reporting lives on `review_suite_arena.py costs --cd <repo>`. It writes `review_cost_ledger.md` in the review-suite state dir and reports T1-T4 reviewer session counts, review wall time, tokens, and estimated cost for the current one-worktree-per-PR lane. Use `--all` for an explicit full backfill over every known worktree in local state. Wrapper-triggered automatic refresh is disabled until it is append-only and cannot call GitHub.
- Runtime choices such as model, reasoning effort, progress cadence, timeouts, and GitHub polling are plugin defaults, not caller knobs.
- State, roster, sqlite, and caller identity options are operator plumbing. Use generated commands when `review-state` emits them.

## Configuration

- Public defaults live in `references/default_config.json`.
- User overrides live in `~/.codex/state/review-suite/config.json`.
- Use config to override plan/deslop/follow-up model defaults and T2/T4 gate primary or fallback reviewer variants.
- `review-t2` and `review-t4` still accept `--champion-override <variant-id>` for one-off operator-directed reviewer selection.

## Privacy

- Arena grading, leaderboards, gate cooldowns, and cost ledgers are local state only.
- Review Suite does not publish local arena telemetry to any external arena service or analytics endpoint.
- Local lanes launch Codex CLI. `review-github` uses GitHub CLI to post and poll `@codex review`.

## State And Scratch

- Workflow anchors live under `~/.codex/state/review-suite/workflow/`.
- State files are internal routing data. Agents should use `review-state status`, not read the JSON directly.
- Keep temporary review notes in a gitignored repo-local `.review-suite/` folder.
- Keep `task_plan.md`, `findings.md`, and `progress.md` in the repo root when using `planning-with-files`.

## Windows + WSL

- If native WSL resolves `codex` to `/mnt/c/.../codex`, local review is not supported there yet.
- Current machine-local fallback: rerun from Windows with `--cd //wsl.localhost/<Distro>/...` and `--wsl`.
- Base-review lanes ignore dirty files outside the committed branch diff and switch to committed merge-base diff review for that run. Dirty files that overlap the branch diff still need a commit or `--allow-dirty`.
