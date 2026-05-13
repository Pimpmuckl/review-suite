---
name: review-t4
description: "Run local PR signoff after `review-t3` is green."
---

# Review T4

Use this only after `review-t3` is green.

```powershell
<python> <review-suite-plugin-root>/scripts/review_t4.py --base main --cd <repo-root>
<python> <review-suite-plugin-root>/scripts/review_t4.py --base main --instructions "Avoid backwards compatibility commentary." --cd <repo-root>
<python> <review-suite-plugin-root>/scripts/review_t4.py --base main --champion-override gpt-5.5-xhigh --cd <repo-root>
```

Use `python` on Windows and usually `python3` on WSL/Linux. If native WSL resolves `codex` to `/mnt/c/.../codex`, rerun from Windows with `--cd //wsl.localhost/<Distro>/...` and `--wsl`. Add `--cd <repo-root>` when launching outside the target repo. Use `--instructions-file <path>` for longer steering text.

Rules:
- This lane is signoff only. Do not grade or benchmark it.
- This is the PR-stage signoff lane. After GitHub-review fixes or post-T4 findings, rerun T4 when `review-state` routes the current stage here; do not step down to T3.
- Reviewer completion leaves the gate in `signoff_pending`; inspect the outputs and run the emitted `close-gate --verdict clean` or `close-gate --verdict findings` command.
- When closing signoff, classify reviewer output as: valid finding, non-finding suggestion/product preference, or unclear product decision. Treat non-finding product/backcompat preferences as effectively green; do not add code guards. If the product decision is genuinely unclear or conflicts with explicit user/product direction, close as findings with a note and escalate before coding.
- Only a `clean` close records the workflow anchor. A `findings` close records the signoff decision but leaves the prior review anchor intact.
- A clean-closed `review-t4` makes local review green for that exact head/base; proceed to `review-github`/CI/merge readiness.
- The wrapper will refuse if `review-state` routes the current branch to `review-followup` or a different full-diff lane.
- If `review-state` routes post-signoff churn back to `review-t4`, rerun this lane as the current-stage full-diff gate.
- Treat findings as symptoms, verify locally, and check adjacent code in the same subsystem.
- For valid substantive findings, write a compact root-cause note and prefer one structurally sound fix over patch-stack guards.
- If the lane surfaces valid findings, close as findings, fix them, route the fix through `review-state`/`review-followup`, then rerun T4 only when `review-state` routes back here.
- Add regression tests for any bugs found.
- Launch this lane after focused, review-relevant validation is green; a completed full-suite/CI run is not required to start review. Record full-suite/CI as pending, passed, failed, or intentionally waived/classified, and do not call a PR final/merge-ready while that state is unknown.
- Only move on once both reviewers are effectively green on the same head and the gate has been closed as `clean`.
- If the shell closes after reviewer completion, recover outputs with `review_suite_arena.py show-last` or `show-round`, then close the gate explicitly.
- Custom instructions are append-only steering. They do not replace the standard findings-only contract.
- On this base-review lane, adding custom instructions switches the wrapper to manual merge-base diff review and uses more tokens than native `--base`.
- If custom instructions make the manual diff artifact too large, remove the custom instructions or split the change before rerunning.
- Use `--champion-override` only for temporary operator-directed reviewer selection.
- Shared dirty files outside the committed branch diff do not require `--allow-dirty`; base review ignores them and reviews the committed diff only.
