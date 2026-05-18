---
name: review-github
description: "Run the anchored GitHub PR review cycle and relay the returned review."
---

# Review GitHub

Use for PR-scoped GitHub review.

```powershell
<python> <review-suite-plugin-root>/scripts/review_github.py run --cd <repo-root>
```

Rules:
- Wait for wrapper output; do not post another request.
- Code only valid findings.
- After fixing findings, run `review-state status` before another review cycle.
- Do not call the PR final/merge-ready until full-suite/CI is passed or waived/classified.
