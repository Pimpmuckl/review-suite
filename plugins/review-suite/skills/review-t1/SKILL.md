---
name: review-t1
description: "Run the graded local slice-review lane."
---

# Review T1

Use this after finishing an implementation slice and before local signoff.

```powershell
<python> <review-suite-plugin-root>/scripts/review_t1.py --base main --cd <repo-root>
<python> <review-suite-plugin-root>/scripts/review_t1.py --commit <sha> --cd <repo-root>
<python> <review-suite-plugin-root>/scripts/review_t1.py --commit <from-sha> <to-sha> --cd <repo-root>
<python> <review-suite-plugin-root>/scripts/review_t1.py --commit <sha> --instructions "Avoid backwards compatibility commentary." --cd <repo-root>
```

Use `python` on Windows and usually `python3` on WSL/Linux. If native WSL resolves `codex` to `/mnt/c/.../codex`, rerun from Windows with `--cd //wsl.localhost/<Distro>/...` and `--wsl`. Prefer `--commit` for a committed slice. Add `--cd <repo-root>` when launching outside the target repo. Use `--instructions-file <path>` for longer steering text.

When the round finishes, grade it with the winner and one basis:
- `valid_findings_vs_none`, `more_valid_findings`, `better_finding_validity`, `better_bug_coverage`
- `false_positive_loss`, `hallucinated_finding_loss`, `fringe_finding_loss`
- `tie_clean`, `tie_both_useful`

Rules:
- This is the graded local slice-review lane.
- Do not use this lane to restart review after the branch has already reached T2, T3, or T4. Run `review-state` and follow its emitted current-stage action instead.
- Do not use `--allow-stage-step-down` just because an amended commit changed HEAD after T2/T3/T4. That override is for an operator-directed exceptional rerun, not normal review recovery.
- Treat findings as symptoms, verify locally, and check adjacent code in the same subsystem.
- For valid substantive findings, write a compact root-cause note with `invariant`, `owner/source_of_truth`, `sibling_paths_checked`, `structural_fix`, and `regression_coverage`.
- Fix structurally, then use `review-followup` before another full rerun unless `review-state status` says the delta is too large.
- Add regression tests for any bugs found.
- Launch this lane after focused, review-relevant validation is green; a completed full-suite/CI run is not required to start review. Record full-suite/CI as pending, passed, failed, or intentionally waived/classified, and do not call a PR final/merge-ready while that state is unknown.
- Before the branch reaches T2, only move on once both reviewers are effectively green on the same head. After T2 has run, do not come back to T1 for a new final-head pass.
- If the round completed and you are deciding between grading and another rerun, use `review-state status` first.
- Pick a winner when one review is materially better on finding validity or bug coverage.
- Use tie only when the reviews are materially indistinguishable.
- If one reviewer has verified findings and the other has none, the reviewer with findings wins by default.
- Before coding from reviewer output, classify each item as: valid finding, non-finding suggestion/product preference, or unclear product decision. Code only valid findings. If reviewer advice conflicts with explicit user/product direction, pause and escalate the tradeoff to the user or parent agent instead of implementing it.
- Do not grade UX preference, product-scope speculation, backwards-compatibility speculation, or alternative product direction as valid findings unless the requirement is explicit. Use `false_positive_loss` or `fringe_finding_loss` when a reviewer wins only by inventing unsupported scope.
- Custom instructions are append-only steering. They do not replace the standard findings-only contract.
- If you add custom instructions to a base review, the wrapper switches to manual merge-base diff review and will use more tokens than native `--base`.
- Shared dirty files outside the committed branch diff do not require `--allow-dirty`; base review ignores them and reviews the committed diff only.
