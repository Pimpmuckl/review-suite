# Review Suite

Review Suite is a Codex plugin for stateful local review, fix verification,
validation tracking, and optional GitHub review.

## Install

```powershell
codex plugin marketplace add Pimpmuckl/review-suite
codex plugin add review-suite@review-suite
```

Refresh an existing installation with:

```powershell
codex plugin marketplace upgrade review-suite
```

## Review modes

| Mode | Use it for | Local review |
| --- | --- | --- |
| `fast` | UI-only, local presentation, and other small, well-tested changes | Dual Sol medium; no deslop, Arena, or GitHub review; at most two local rounds |
| `normal` | Everything else | Deslop sidecar, configured phase Arena rounds when enabled, then dual Sol medium until green and GitHub review |
| `deep` | Billing, login/auth, security, business-critical systems, database integrity/migrations, concurrency, and similarly critical logic | Deslop sidecar, dual Sol medium until green, configured deep Arena rounds when enabled, dual Sol xhigh until green, then GitHub review |

These are risk heuristics, not permission to downgrade a UI-looking change that
crosses a trust or data-integrity boundary.

## Run a review

```powershell
<python> <plugin-root>/scripts/review.py --cd <repo-root>
```

Omitting `--mode` creates a `normal` review. Pass `--mode fast` or `--mode deep`
only when the risk warrants it. Review Suite detects the remote default branch;
use `--base <ref>` only to override it explicitly.

The first call creates or reconnects a review and prints one `Action.cmd`.
Follow that command until the review is green or requires a code fix. After a
review id exists, the normal continuation is:

```powershell
<python> <plugin-root>/scripts/review.py --id <id>
```

Useful read-only checks:

```powershell
<python> <plugin-root>/scripts/review.py --status --cd <repo-root>
<python> <plugin-root>/scripts/review.py --id <id> --show-status
<python> <plugin-root>/scripts/review.py --id <id> --show-findings
```

Review orchestration expects committed changes and a clean worktree. Run
focused validation before review dispatch, then track the full suite and CI on
the review id. Review green does not mean merge-ready while required validation
is pending or unknown.

To replace an active review with a stricter one:

```powershell
<python> <plugin-root>/scripts/review.py --id <id> --restart-mode deep --reason "why deeper review is required"
```

When a review exhausts its local round budget, its emitted action can start one
same-mode successor without repeating the repository context:

```powershell
<python> <plugin-root>/scripts/review.py --id <id> --new-cycle
```

## Discovery and Arena

Stable profiles do not run discovery brawls. Discovery pools and ratings remain
available for deliberate calibration.

Arena is an opt-in evaluation overlay. Normal and deep run their configured
Arena counts only when Arena is enabled and the count is positive. The calling
agent grades the outputs; Review Suite never selects or promotes a winner.
Each reporting pool uses its balanced groups once, then favors under-sampled
candidates and opponents they have met least often. When both cohorts can fill
half a group, bootstrap rounds mix under-sampled and established candidates
evenly. New candidates join the existing pool at 1500 Elo without resetting
established ratings.

User configuration lives at:

```text
~/.codex/state/review-suite/config.json
```

The shipped defaults are in
`plugins/review-suite/references/default_config.json`. Review history, ratings,
and orchestration state remain under `~/.codex/state/review-suite/`.

## Skills

- `review`: local review, continuation, and status.
- `review-plan`: review a written implementation plan.
- `review-deslop`: one-off simplification review.
- `review-github`: anchored GitHub pull-request review.

## Development

Requirements: Python 3.14.6+, `uv`, Codex CLI, Git, and GitHub CLI for GitHub
review.

```powershell
uv sync
uv run pytest plugins/review-suite/tests -q
uv run ruff format --check .
uv run ruff check .
```

After source changes, sync the installed plugin cache and marketplace source:

```powershell
.\scripts\sync-installed-cache.ps1
```

Installed launchers create content-addressed runtime copies under
`~/.codex/plugin-runtimes/review-suite/`. Run commands through the installed
plugin cache by default. When reviewing Review Suite itself, use the current
source checkout only if the user explicitly requests dogfooding unsynced source
changes. Never use the temporary marketplace clone.
