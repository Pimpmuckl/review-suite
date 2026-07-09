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
- Python 3.14.6+
- `uv`
- `git`
- GitHub CLI `gh` only for `review-github`

## Quick Start

Use `$review` for local review/status and `$review-github` for PR-scoped GitHub review.

Default local runs print reviewer text once in `Output:`, then one `Action.cmd`.

Review orchestration expects committed review changes. If `git diff` is non-empty but `base..HEAD` is empty, commit the intended changes or stash unrelated worktree changes before rerunning the emitted command.
If you accidentally repeat `--mode` after amending fixes on the same branch/base/merge-base, Review Suite reconnects to the active review id instead of starting a fresh ladder. Use `--fresh-token` only when you intentionally want a separate ladder.
Review commands do not support a dirty-worktree override. Do not append `--allow-dirty`; commit intended review changes first.
After a review id exists, the normal advance command is bare `review.py --id <id>`. Review Suite records structured reviewer `clean` / `findings` verdicts automatically when available, runs the next safe step, and leaves explicit `--decision clean|findings` as a manual override for ambiguous or intentional human judgment cases.
Inspect an existing id without advancing it with `review.py --id <id> --show-status`.

Modes are built from one phased stack. Discovery uses GPT 5.4 for high-recall bug finding; signoff uses GPT 5.6 Sol for relevance, convergence, and current-head green checks.
Deslop passes are folded into any review that isn't using `emergency` as target.

Arena loops are backend-injected by `review.py` only when user config opts in with `arena.enabled` plus a nonzero `normal_arena_loops` or `deep_arena_loops` budget.
When enabled, arena loops spend discovery budget first. Each discovery phase still keeps at least one fixed GPT 5.4 pass as the safety net. Agents should keep following `review.py` actions; the only arena-specific agent action is grading an arena round when prompted.

```text
|---- Emergency Phase ----
|
|--- Urgent Signoff - Single fast batch, max two rounds after findings
     |- GPT 5.6 Sol Medium x2
```

Emergency turns green immediately on a clean urgent signoff. If findings are fixed, it allows one verification rerun; findings after that exhaust the local review budget instead of launching more reviewers.

```text
|--- Brief / Normal Phase ----
|    |
|    |--- Medium Discovery - Until: normal_discovery_loops
|    |    |- Brief:  GPT 5.4 Medium x4, fixed fast pass
|    |    |- Normal: optional backend-injected phase arena rounds
|    |    |- Normal: GPT 5.4 Medium x4 for remaining passes, min once
|    |
|    |--- Medium Signoff - Until: Green
|    |    |- GPT 5.6 Sol Medium x2
|
|
|--- Deep Phase ----
|    |
|    |--- Brief / Normal stack first
|    |    |- Medium Discovery
|    |    |- Medium Signoff
|    |
|    |--- Deep Discovery - Until: deep_discovery_loops
|    |    |- optional backend-injected PR arena rounds
|    |    |- GPT 5.4 XHigh x2 for remaining passes, min once
|    |
|    |--- Deep Signoff - Until: Green
|    |    |- GPT 5.6 Sol XHigh x2
|
|
|--- Github Phase ----
     |--- GitHub - Until: Green/Waived
          |- findings -> fix -> follow-up -> rerun Deep Signoff
```

To explicitly replace an existing local review ladder with a stricter one, use the selected review id:

```powershell
python <review-suite-plugin-root>/scripts/review.py --id rvw_xxx --restart-mode deep --reason "why this run needs stricter review"
```

`--restart-mode` only allows strictness escalation (`brief` to `normal`/`deep`, or `normal` to `deep`). The old review is marked superseded, and the replacement review starts as a fresh ladder for the same repo/base/branch/head/merge-base. The worktree must be clean.

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

The installed cache stays a launcher surface. Runtime commands re-exec from the runtime copy, but emitted `Action.cmd` values point back at the installed launcher so old inactive runtime directories can be removed when Windows allows it. Existing review state remains under `~/.codex/state/review-suite`.

## Development

Sync dependencies:

```powershell
uv sync
```

uv ignores package files uploaded in the last week.

Run tests:

```powershell
uv run pytest plugins/review-suite/tests -q
```

Compile scripts:

```powershell
$files = @(
  Get-ChildItem -LiteralPath plugins/review-suite/scripts -Filter *.py -File
  Get-ChildItem -LiteralPath plugins/review-suite/scripts/review_suite_core -Filter *.py -File
)
uv run python -m py_compile @($files.FullName)
```

Sync the local installed plugin cache after source edits. When the git marketplace source clone exists, the default command syncs that clone too so local plugin add/refresh paths see the same files:

```powershell
.\scripts\sync-installed-cache.ps1
```

Sync only the git marketplace source clone used by `codex plugin marketplace add`:

```powershell
.\scripts\sync-installed-cache.ps1 -MarketplaceSource
```
