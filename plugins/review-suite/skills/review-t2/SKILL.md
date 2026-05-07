---
name: review-t2
description: "Run local slice signoff after `review-t1` is green."
---

# Review T2

Use this only after `review-t1` is green. If the branch already reached T2 and HEAD changes, do not rerun T1; run `review-state` and follow its current-stage action.

```powershell
<python> <review-suite-plugin-root>/scripts/review_t2.py --base main --cd <repo-root>
<python> <review-suite-plugin-root>/scripts/review_t2.py --commit <sha> --cd <repo-root>
<python> <review-suite-plugin-root>/scripts/review_t2.py --commit <from-sha> <to-sha> --cd <repo-root>
<python> <review-suite-plugin-root>/scripts/review_t2.py --commit <sha> --instructions "Avoid backwards compatibility commentary." --cd <repo-root>
<python> <review-suite-plugin-root>/scripts/review_t2.py --base main --champion-override gpt-5.5-medium --cd <repo-root>
```

Use `python` on Windows and usually `python3` on WSL/Linux. If native WSL resolves `codex` to `/mnt/c/.../codex`, rerun from Windows with `--cd //wsl.localhost/<Distro>/...` and `--wsl`. Prefer `--commit` for a committed slice. Add `--cd <repo-root>` when launching outside the target repo. Use `--instructions-file <path>` for longer steering text.

Rules:
- This lane is signoff only. Do not grade or benchmark it.
- Do not use this lane after the branch has already reached T3 or T4 unless `review-state` routes the current stage back here.
- Do not require a fresh T1 after amended commits once T2 has already run. Current-stage T2 signoff is the relevant local slice gate.
- Reviewer completion leaves the gate in `signoff_pending`; inspect the outputs and run the emitted `close-gate --verdict clean` or `close-gate --verdict findings` command.
- When closing signoff, treat verified product-scope/backcompat false positives as effectively green; do not add code guards. If product scope is genuinely unclear, close as findings with a note and escalate before coding.
- Only a `clean` close records the workflow anchor. A `findings` close records the signoff decision but leaves the prior review anchor intact.
- On base/branch signoff, the wrapper will refuse if `review-state` routes the branch to `review-followup` or a different full-diff lane.
- If `review-state` routes post-signoff churn back to `review-t2`, rerun this lane as the current-stage full-diff gate.
- Treat findings as symptoms, verify locally, and check adjacent code in the same subsystem.
- For valid substantive findings, write a compact root-cause note and prefer one structurally sound fix over patch-stack guards.
- If the lane surfaces valid findings, close as findings, fix them, route the fix through `review-state`/`review-followup`, then rerun T2 only when `review-state` routes back here.
- Add regression tests for any bugs found.
- Only move on from T2 once both T2 reviewers are effectively green on the same head and the gate has been closed as `clean`.
- If the shell closes after reviewer completion, recover outputs with `review_suite_arena.py show-last` or `show-round`, then close the gate explicitly.
- Custom instructions are append-only steering. They do not replace the standard findings-only contract.
- If you add custom instructions to a base review, the wrapper switches to manual merge-base diff review and will use more tokens than native `--base`.
- If custom instructions make the manual diff artifact too large, remove the custom instructions or split the change before rerunning.
- Use `--champion-override` only for temporary operator-directed reviewer selection.
- Shared dirty files outside the committed branch diff do not require `--allow-dirty`; base review ignores them and reviews the committed diff only.
