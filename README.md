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

Review orchestration expects committed review changes. If `git diff` is non-empty but `base..HEAD` is empty, commit the intended changes or stash unrelated worktree changes before rerunning the emitted command.

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

## Runtime Copies

Installed cache paths are launcher paths. A git marketplace install usually launches from:

```text
~/.codex/plugins/cache/review-suite/review-suite/<version>/
```

Older local marketplace installs may launch from `~/.codex/plugins/cache/jonat-local/review-suite/local/`. Do not invoke scripts directly from `~/.codex/.tmp/marketplaces/review-suite`; that directory is Codex's marketplace source clone, not the stable plugin launcher surface.

When Review Suite is launched from Codex's installed plugin cache, long-running entrypoints create or reuse a runtime copy under:

```text
~/.codex/plugin-runtimes/review-suite/<version-hash>/
```

The installed cache stays a launcher surface. Emitted `Action.cmd` values may point at the runtime copy after bootstrap; that is expected and avoids locking the installed cache on Windows. Existing review state remains under `~/.codex/state/review-suite`, and old runtime directories are not aggressively removed while Codex sessions may still be using them.

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

Sync the git marketplace source clone used by `codex plugin marketplace add`:

```powershell
.\scripts\sync-installed-cache.ps1 -MarketplaceSource
```
