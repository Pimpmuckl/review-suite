# Review strategy

Public modes describe risk, not model experiments:

| Mode | Use | Ladder |
| --- | --- | --- |
| `fast` | UI-only, local presentation, and other small, well-tested changes | Dual configured normal-model signoff, then bounded exact-head closure; at most two local rounds |
| `normal` | Everything else | Optional phase Arena rounds, dual configured normal-model signoff until green, bounded exact-head closure, GitHub review |
| `deep` | Billing, authentication/login, authorization/security, database integrity or migrations, concurrency, and similarly critical logic | Dual configured normal-model signoff until green, optional deep Arena rounds, dual configured deep-model signoff until green, bounded exact-head closure, GitHub review |

Omitting `--mode` creates a `normal` review. Risk wins over labels: a UI change
that crosses a trust or data-integrity boundary is not `fast`.

Arena is opt-in. Stable profiles omit Arena steps when it is disabled or their
configured loop count is zero. Discovery pools, historical ratings, and manual
calibration remain available, but stable profiles do not run fixed discovery
brawls. The caller grades Arena output; Review Suite does not automatically
select or promote models.

With the shipped model defaults, the sequences are:

```text
fast    2x Astra medium -> cleanup/conformance (Astra medium) -> done
normal  Arena -> 2x Astra medium -> cleanup/conformance (Astra medium) -> GitHub
deep    2x Astra medium -> Arena -> 2x Astra xhigh
          -> cleanup/conformance (Astra medium) -> GitHub
```

Arena uses its mixed-model roster when enabled. GitHub review uses the GitHub
Codex service; these model settings do not select its model. Findings require
fixes and repeat review before advancing. Cleanup changes also require renewed
correctness review.

Earlier review passes, plan review, follow-up, and cleanup use `normal`. The
final correctness signoff in deep mode uses `deep`, unless overridden per job.
See [model settings](../README.md#settings) and the [Arena maintainer guide](arena.md).
