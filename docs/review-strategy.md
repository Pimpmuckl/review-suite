# Review strategy

This document records the review-mode direction while GPT-5.6 discovery and
Arena collect enough evidence to simplify the production workflow safely.

## Design principle

A public mode should answer one question: how much scrutiny does this change
need? Model selection, discovery repetition, and Arena sampling belong to the
configured profile rather than the caller-facing mode vocabulary.

The intended long-term public surface is:

| Mode | Contract |
| --- | --- |
| `fast` | Focused Sol medium signoff for small, localized, well-tested changes |
| `normal` | Default phase review followed by Sol medium signoff |
| `deep` | High-risk review using deep discovery, Sol xhigh signoff, and GitHub review when required |

`brief` remains available during the current evaluation as a useful cost
control. It should eventually collapse into `normal`; changing the number of
discovery passes is configuration, not a durable product concept.

## Current decisions

- `fast` replaces the old `emergency` name as a clean break. There is no alias.
- `normal` is the default for behavior changes.
- `deep` is for stateful, cross-system, security, concurrency, migration, and
  other high-blast-radius work.
- Discovery remains active while GPT-5.4, GPT-5.5, GPT-5.6 Luna, Terra, and Sol
  are being evaluated.
- Only the calling agent grades discovery and Arena placements. There is no
  automatic winner or promotion rule.
- Phase and deep discovery may reach different conclusions.

## What discovery must prove

Elo alone does not decide whether a model deserves a production discovery
slot. The useful question is whether it contributes accepted findings that Sol
signoff would otherwise miss.

At each review checkpoint, inspect:

- unique valid findings not found by Sol signoff;
- duplicate or corroborating findings;
- cost per unique valid finding;
- low-quality and scope-bloat losses;
- whether the model repeatedly contributes a different class of reasoning.

Higher-effort Luna or Terra variants may be valuable even when they are not the
overall Elo leader. For example, Luna max or Terra xhigh could become a phase
discovery specialist if it reliably finds different issues from Sol medium.

## Decision checkpoint

Do not simplify discovery before both phase and deep have useful coverage. The
first formal checkpoint requires:

- one complete 13-event Arena schedule in each pool; and
- at least ten caller-graded discovery brawls in each discovery pool.

If the evidence is close, run another cycle. Do not keep the evaluation shape
forever merely because the result is inconclusive.

## Possible outcomes

### Superseded

Sol signoff covers the useful findings. Remove production discovery from that
review depth while keeping Arena available for deliberate future calibration.

### Specialist

One model contributes sufficiently different findings. Replace the scramble
with one configured discovery specialist before Sol signoff.

### Portfolio

Multiple models remain complementary. Keep a small configured rotation rather
than treating one global Elo winner as universally best.

## Later simplifications

After the discovery decision:

1. Remove `brief` and make `normal` the single everyday profile.
2. Configure `normal` with either no discovery, one specialist, or the smallest
   useful rotation supported by the evidence.
3. Reassess whether `deep` still needs to execute the entire normal stack first.
   A focused deep specialist plus Sol xhigh signoff may provide the same useful
   coverage with substantially fewer model calls.
4. Update this document with the decision and the evidence window used.
