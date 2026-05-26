---
name: review-github
description: "Run the anchored GitHub PR review cycle and relay the returned review."
---

# Review GitHub

Use for PR-scoped GitHub review.

```powershell
<python> <review-suite-plugin-root>/scripts/review.py --id <id> --github-review
```

`<review-suite-plugin-root>` is the installed Codex plugin cache root, not `%USERPROFILE%\.codex\.tmp\marketplaces\review-suite`.

Rules:
- Wait for wrapper output; do not post another request.
- Code only valid findings.
- Record the GitHub result on the owning local review id: `review.py --id <id> --github-result clean|findings`, or `--github-result waived --github-note "why"` when GitHub cannot run or no-GitHub is approved.
- After `--github-result findings`, fix the issue and follow the emitted `review.py --id <id>` actions; the same id reruns final local signoff before GitHub is requested again.
- Do not call the PR final/merge-ready until full-suite/CI is passed or waived/classified.
