# Review Suite

Review Suite is a Codex plugin for stateful code review. One local orchestrator
runs discovery, signoff, fix verification, validation tracking, and optional
GitHub review without making the calling agent manage individual reviewer
rounds.

## Install

```powershell
codex plugin marketplace add https://github.com/Pimpmuckl/review-suite --ref main
codex plugin add review-suite@review-suite
```

Refresh an existing installation with:

```powershell
codex plugin marketplace upgrade review-suite
```

## Review modes

| Mode | Use it for | Local review |
| --- | --- | --- |
| `fast` | Small, localized, well-tested changes | GPT-5.6 Sol medium signoff only; no deslop; at most two review rounds |
| `brief` | Cost-controlled review while model evaluation is active | One four-model medium discovery brawl, then Sol medium signoff |
| `normal` | Default behavior changes | Medium discovery budget, optional phase arena round, then Sol medium signoff |
| `deep` | Stateful, cross-system, security, concurrency, and migration work | Normal stack, xhigh discovery, optional deep arena round, Sol xhigh signoff, and GitHub review when required |

Use `normal` unless the change is clearly small enough for `fast` or risky
enough for `deep`. The current discovery evaluation and the intended future
three-mode shape are documented in [Review strategy](docs/review-strategy.md).

## Run a review

```powershell
<python> <plugin-root>/scripts/review.py --mode fast|brief|normal|deep --cd <repo-root>
```

Review Suite detects the remote default branch. Use `--base <ref>` only to
override it explicitly.

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

## Discovery and Arena

Phase discovery currently compares GPT-5.4, GPT-5.5, GPT-5.6 Sol, and GPT-5.6
Terra at medium effort. Deep discovery compares the same families at xhigh.
GPT-5.6 Sol remains the final signoff model.

Arena is an opt-in evaluation overlay. When enabled, it replaces part of the
discovery budget with configured four-model events. The calling agent grades
ordered placements and ties after checking the reviewer output; Review Suite
does not infer winners from finding counts or output order.

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
plugin cache, not the temporary marketplace clone.
