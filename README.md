# Review Suite

Review Suite is a Codex plugin for stateful review workflows:

- plan review
- deslop review
- local review orchestration
- review progress/status checks
- anchored GitHub `@codex review` polling

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

Use `$review` for local review, `$review-state` to inspect progress, and `$review-github` for PR-scoped GitHub review.

Default local runs print reviewer text once in `Output:`, then one `Action.cmd`.

Modes:

- `brief`: tiny same-seam fix, docs/wording, focused regression proves it
- `normal`: normal backend/runtime seam with no durable lifecycle or data-ownership change
- `deep`: leases, retries, queues, terminal state, schema/data ownership, or security
- `emergency`: blocked stack/local run; fix now and soak after

## Configuration

Default configuration:

```text
plugins/review-suite/references/default_config.json
```

User override:

```text
~/.codex/state/review-suite/config.json
```

## Privacy And Network

Review Suite keeps review state and cost ledgers under the local review-suite state directory.

The plugin does not publish arena telemetry to any external arena service or analytics endpoint.

Network behavior:

- local review workflows launch Codex CLI review sessions
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
