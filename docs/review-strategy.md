# Review strategy

Public modes describe risk, not model experiments:

| Mode | Use | Ladder |
| --- | --- | --- |
| `fast` | UI-only, local presentation, and other small, well-tested changes | Dual Sol medium, at most two local rounds |
| `normal` | Everything else | Deslop sidecar, optional configured phase Arena rounds, dual Sol medium until green, GitHub review |
| `deep` | Billing, authentication/login, authorization/security, database integrity or migrations, concurrency, and similarly critical logic | Deslop sidecar, dual Sol medium until green, optional configured deep Arena rounds, dual Sol xhigh until green, GitHub review |

Omitting `--mode` creates a `normal` review. Risk wins over labels: a UI change
that crosses a trust or data-integrity boundary is not `fast`.

Arena is opt-in. Stable profiles omit Arena steps when it is disabled or their
configured loop count is zero. Discovery pools, historical ratings, and manual
calibration remain available, but stable profiles do not run fixed discovery
brawls. The caller grades Arena output; Review Suite does not automatically
select or promote models.
