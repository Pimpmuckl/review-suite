---
name: review-followup
description: "Review only the fix interdiff after a valid finding."
---

# Review Followup

Use after fixing a valid reviewer finding.

```powershell
<python> <review-suite-plugin-root>/scripts/review_followup.py --base main --cd <repo-root> --note-file .review-suite/fix-note.md
<python> <review-suite-plugin-root>/scripts/review_followup.py --base main --cd <repo-root> --since <reviewed-head> --note "<compact RCA note>"
```

Rules:
- Ungraded lane.
- Reviews only `<last_reviewed_head>..HEAD`.
- Include a compact root-cause note: invariant, owner/source, sibling paths, structural fix, regression coverage.
- If the wrapper rejects the delta, run `review-state status`.
- Use `--force` only for an intentional guard override.
- Launch after focused validation for the fix is green.
