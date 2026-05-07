---
name: review-t3
description: "Run the graded local PR-ready review lane."
---

# Review T3

Use this when the branch is PR-ready and `review-t2` is already green.

```powershell
<python> <review-suite-plugin-root>/scripts/review_t3.py --base main --cd <repo-root>
<python> <review-suite-plugin-root>/scripts/review_t3.py --base main --instructions "Avoid backwards compatibility commentary." --cd <repo-root>
```

Use `python` on Windows and usually `python3` on WSL/Linux. If native WSL resolves `codex` to `/mnt/c/.../codex`, rerun from Windows with `--cd //wsl.localhost/<Distro>/...` and `--wsl`. Add `--cd <repo-root>` when launching outside the target repo. Use `--instructions-file <path>` for longer steering text.

When the round finishes, grade it with the winner and one basis:
- `valid_findings_vs_none`, `more_valid_findings`, `better_finding_validity`, `better_bug_coverage`
- `false_positive_loss`, `hallucinated_finding_loss`, `fringe_finding_loss`
- `tie_clean`, `tie_both_useful`

Rules:
- This is the graded local PR-ready review lane.
- Do not use this lane to restart review after the branch has already reached T4. Run `review-state` and follow its emitted current-stage action instead.
- Treat findings as symptoms, verify locally, and check adjacent code in the same subsystem.
- For valid substantive findings, write a compact root-cause note with `invariant`, `owner/source_of_truth`, `sibling_paths_checked`, `structural_fix`, and `regression_coverage`.
- Fix structurally, then use `review-followup` before another PR-wide rerun unless `review-state status` escalates.
- Add regression tests for any bugs found.
- Only move on once both reviewers are effectively green on the same head.
- If the round completed and you are deciding between grading and another rerun, use `review-state status` first.
- Pick a winner when one review is materially better on finding validity or bug coverage.
- Use tie only when the reviews are materially indistinguishable.
- If one reviewer has verified findings and the other has none, the reviewer with findings wins by default.
- Do not grade product-scope or backwards-compatibility assumptions as valid findings unless the requirement is explicit. Use `false_positive_loss` or `fringe_finding_loss` when a reviewer wins only by inventing unsupported scope.
- Custom instructions are append-only steering. They do not replace the standard findings-only contract.
- On this base-review lane, adding custom instructions switches the wrapper to manual merge-base diff review and uses more tokens than native `--base`.
- Shared dirty files outside the committed branch diff do not require `--allow-dirty`; base review ignores them and reviews the committed diff only.
