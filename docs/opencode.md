# OpenCode review backend

Review Suite can run local reviewers through OpenCode without changing the review workflow or result contract.

Use an explicit `opencode::` model prefix. The remainder is passed to OpenCode as its normal `provider/model` identifier:

```toml
[normal]
model = "opencode::opencode-go/deepseek-flash"
reasoning = "medium"
```

For GLM-5.3-Flash, only the model value changes:

```toml
[normal]
model = "opencode::opencode-go/glm-5.3-flash"
reasoning = "medium"
```

The `reasoning` field remains required by Review Suite's model contract, but OpenCode uses the provider's default reasoning behavior. If a provider exposes a native OpenCode model variant, encode it in the OpenCode model identifier rather than assuming Codex reasoning levels map one-for-one.

## Runtime contract

The backend deliberately shares the existing Review Suite review prompt, target selection, terminal `Review result: clean|findings` protocol, classification, retries, orchestration, and Arena grading.

OpenCode is launched through a small adapter with a Review Suite-owned primary review agent. The adapter:

- runs `opencode --pure run` non-interactively;
- injects a review system prompt that mirrors Codex review's actionable-defect criteria while leaving Review Suite's output contract authoritative;
- denies shell, edits, subagents/tasks, web access, skills, questions, and external-directory access;
- allows only repository read/search/LSP tools;
- generates the bounded target patch itself and attaches it to the OpenCode run, so the model never needs shell access to inspect the diff;
- captures the final completed JSON text step and falls back to `opencode export <session>` if OpenCode finishes without emitting the final text event. Completed responses without the terminal result marker remain available for caller classification, as on the Codex path; intermediate tool steps and compaction summaries are not substituted for a review.

This intentionally keeps provider quirks inside the backend. Adding another OpenCode Go model should normally be a model/configuration change, not a new execution path.

## Prerequisites

Install and authenticate the OpenCode CLI normally, including the OpenCode Go subscription/provider. Review Suite does not copy or manage OpenCode credentials.

`service_tier` is Codex-specific and is rejected for OpenCode-backed reviewers rather than silently ignored.
