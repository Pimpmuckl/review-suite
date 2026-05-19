# Review Suite

Review Suite is a Codex plugin for stateful review workflows:

- plan review
- post-implementation simplification review
- fix follow-up review
- default stateful local review orchestration
- specialist local review and signoff lanes
- anchored GitHub `@codex review` polling
- local review state and cost reporting

## Install

Add this repository as a Codex plugin marketplace:

```powershell
codex plugin marketplace add https://github.com/Pimpmuckl/review-suite --ref main
codex plugin add review-suite@review-suite
```

Refresh later with:

```powershell
codex plugin marketplace upgrade review-suite
```

## Requirements

- Codex CLI available as `codex`
- Python 3.11+
- `git`
- GitHub CLI `gh` only for `review-github`

## Quick Start

Use `$review` for local review, `$review-suite` when unsure which lane fits, and `$review-github` for PR-scoped GitHub review.

Default local runs print reviewer text once in `Output:`, then one `Action.cmd`.

## Configuration

Default configuration lives in the plugin at:

```text
plugins/review-suite/references/default_config.json
```

Users can override it with:

```text
~/.codex/state/review-suite/config.json
```

Typical overrides:

```json
{
  "lens": {
    "default": {
      "model": "gpt-5.5",
      "reasoning_effort": "medium"
    }
  },
  "gates": {
    "phase_gate": {
      "primary_variant_ids": ["gpt-5.4-medium"]
    },
    "pr_gate": {
      "primary_variant_ids": ["gpt-5.5-xhigh"]
    }
  }
}
```

T2/T4 also support `--champion-override <variant-id>` for explicit operator overrides.

## Privacy And Network

Review Suite keeps arena state local. T1/T3 grading, local leaderboards, gate cooldowns, and cost ledgers are written under the local review-suite state directory.

The plugin does not publish arena telemetry to any external arena service or analytics endpoint.

Network behavior:

- local review lanes launch Codex CLI review sessions
- `review-github` uses `gh` to post and poll `@codex review`
- no other external publishing is part of the public plugin

## Development

Run tests:

```powershell
python -m pytest plugins/review-suite/tests -q
```

Compile scripts:

```powershell
python -m py_compile plugins/review-suite/scripts/*.py plugins/review-suite/scripts/review_suite_core/*.py
```

Sync a local installed plugin cache after source edits:

```powershell
.\scripts\sync-installed-cache.ps1
```
