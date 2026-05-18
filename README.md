# Review Suite

Review Suite is a Codex plugin for stateful review workflows:

- plan review
- post-implementation simplification review
- fix follow-up review
- local T1-T4 review gates
- anchored GitHub `@codex review` polling
- local review state and cost reporting

## Install

Add this repository as a Codex plugin marketplace:

```powershell
codex plugin marketplace add https://github.com/Pimpmuckl/review-suite --ref main
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

Use `$review-suite` to choose the right lane, or call a specific skill:

- `$review-plan`
- `$review-deslop`
- `$review-state`
- `$review-followup`
- `$review-t1`
- `$review-t2`
- `$review-t3`
- `$review-t4`
- `$review-github`

The plugin stores local routing state under:

```text
~/.codex/state/review-suite/
```

Default review command output is compact for agent callers. Long-running reviews emit a sparse `OK <minutes>m: ...` heartbeat about every 60s. Reviewer completion lines are status-only. Full stored reviewer bodies are available on demand through `review_suite_arena.py show-round` or `show-last`.

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
