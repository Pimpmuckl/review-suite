---
name: review-github
description: "Run the anchored GitHub PR review cycle and relay the returned review."
---

# Review GitHub

Use this only for PR-scoped GitHub review cycles.

```powershell
<python> <review-suite-plugin-root>/scripts/review_github.py run --cd <repo-root>
```

Use `python` on Windows and usually `python3` on WSL/Linux. Add `--cd <repo-root>` when launching outside the target repo or when PR inference should resolve against a specific repo.

Rules:
- The wrapper owns the request and anchored polling cycle.
- GitHub review can take up to 30 minutes; wait for wrapper output instead of posting another request.
- Treat returned findings like any other reviewer output: validate locally and fix structurally.
- After a valid substantive finding, write the compact root-cause note, run `review-state status`, and use `review-followup` or coherence/reset locally before another GitHub cycle.
