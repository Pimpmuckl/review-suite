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
| `fast` | UI-only, local presentation, and other small, well-tested changes | Dual normal-model signoff until green, then bounded exact-head closure; no GitHub review; at most two local rounds |
| `normal` | Everything else | Dual normal-model signoff until green, bounded exact-head closure, then GitHub review |
| `deep` | Billing, login/auth, security, business-critical systems, database integrity/migrations, concurrency, and similarly critical logic | Dual normal-model signoff until green, a second dual signoff until green, bounded exact-head closure, then GitHub review |

These are risk heuristics, not permission to downgrade a UI-looking change that
crosses a trust or data-integrity boundary.

## Run a review

```powershell
<python> <plugin-root>/scripts/review.py --cd <repo-root>
```

Omitting `--mode` creates a `normal` review. Pass `--mode fast` or `--mode deep`
only when the risk warrants it. Review Suite detects the remote default branch;
use `--base <ref>` only to override it explicitly.

The first call creates or reconnects a review and prints an `Action`. Run its
`cmd`, or classify the review output and run exactly one matching `choices`
command. After a review id exists, the normal continuation is:

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

To dismiss an erroneous bounded closure verdict on the same clean head:

```powershell
<python> <plugin-root>/scripts/review.py --id <id> --deslop-done --reason "why the findings are dismissed"
```

The reason is saved alongside the unchanged reviewer verdict and findings.
This closes only the completed closure pass, not other review or validation gates.
Without a reason, a materially drifted closure remains blocked.

If an earlier `CONTINUE` left a completed review asking for a fix with no
findings, repeat the same decision to restore its pending classification:

```powershell
<python> <plugin-root>/scripts/review.py --id <id> --convergence-decision continue
```

This does not grant another extension or bypass review and validation gates.

To replace an active review with a stricter one:

```powershell
<python> <plugin-root>/scripts/review.py --id <id> --restart-mode deep --reason "why deeper review is required"
```

When a review exhausts its local round budget, its emitted action can start one
same-mode successor without repeating the repository context:

```powershell
<python> <plugin-root>/scripts/review.py --id <id> --new-cycle
```

## Settings

Shipped defaults live at the plugin root in
[`default_settings.toml`](plugins/review-suite/default_settings.toml):

```toml
[normal]
model = "gpt-6-astra"
reasoning = "medium"

[deep]
model = "gpt-6-astra"
reasoning = "xhigh"
```

Discovery and earlier review passes use `normal`. The final signoff in deep
mode and PR-gate signoff use `deep`. Per-job overrides still take precedence.

Optional user overrides live at:

```text
~/.codex/state/review-suite/settings.toml
```

The first run creates a comment-only stub. **Defaults are never copied into
user settings**, so updating the plugin also updates every setting you have
not overridden. Add only the fields you want to pin:

```toml
[jobs.deslop]
model = "gpt-5.6-luna" # inherits normal reasoning

[jobs.plan]
reasoning = "high" # inherits the normal model

```

Job names: `plan`, `deslop`, `followup`, `phase_discovery`, `pr_discovery`,
`normal_signoff`, and `deep_signoff`. Each can override `model`, `reasoning`,
and `service_tier` (`fast` or `flex`; empty string clears an inherited tier).
Partial job overrides inherit from their normal/deep group.

If only legacy `config.json` exists, the first run automatically converts its
non-model settings to TOML. Old model names, reasoning choices, model references,
and model lists are discarded so they cannot pin an obsolete model. Counts,
loop settings, and other non-model settings are retained. The old JSON stays intact as a backup and is ignored once TOML
exists; subsequent explicit TOML model overrides are respected.

Review history, ratings, and orchestration state stay under
`~/.codex/state/review-suite/`.

## Skills

- `review`: local review, continuation, and status.
- `review-plan`: review a written implementation plan.
- `review-deslop`: one-off simplification review.
- `review-github`: anchored GitHub pull-request review.

## Development

Maintainer documentation: [Arena configuration](docs/arena.md).


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
`~/.codex/plugin-runtimes/review-suite/` without writing launcher bytecode into
the installed plugin cache. Run commands through the installed plugin cache by
default. When reviewing Review Suite itself, use the current source checkout
only if the user explicitly requests dogfooding unsynced source changes. Never
use the temporary marketplace clone.
