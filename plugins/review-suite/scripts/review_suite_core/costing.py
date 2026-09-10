from __future__ import annotations

from typing import Any


def normalize_usage_tokens(value: dict[str, Any] | None) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    input_details = value.get("input_tokens_details")
    if not isinstance(input_details, dict):
        input_details = {}
    usage = {
        "input_tokens": int(value.get("input_tokens", 0) or 0),
        "cached_input_tokens": int(
            value.get("cached_input_tokens", input_details.get("cached_tokens", 0)) or 0
        ),
        "output_tokens": int(value.get("output_tokens", 0) or 0),
    }
    if "cache_write_tokens" in value or "cache_write_tokens" in input_details:
        usage["cache_write_tokens"] = int(
            value.get("cache_write_tokens", input_details.get("cache_write_tokens", 0))
            or 0
        )
    return usage


def core_usage_tokens(usage: dict[str, Any] | None) -> int:
    """Return the shared comparison token total for a run.

    Cache reads are reused context and are excluded. Cache writes remain part of
    input because they are freshly processed prompt tokens. Reasoning tokens are
    billed as output and are folded into output. This is the token total shown by
    both the Arena leaderboard and the review cost ledger.
    """
    if not isinstance(usage, dict):
        return 0
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    cached_input_tokens = int(usage.get("cached_input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    reasoning_tokens = int(usage.get("reasoning_output_tokens", 0) or 0)
    return max(0, input_tokens - cached_input_tokens) + output_tokens + reasoning_tokens


_USAGE_TOKEN_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "cache_write_tokens",
    "reasoning_output_tokens",
)


def _has_structured_usage(usage: Any) -> bool:
    return isinstance(usage, dict) and any(key in usage for key in _USAGE_TOKEN_KEYS)


def run_total_tokens(run: dict[str, Any] | None) -> int:
    """Return the shared token total for a stored run record.

    Uses the usage components when structured usage is present, even if the
    computed total is zero, so a recorded usage breakdown is always authoritative.
    Falls back to the provider-reported total only when usage is missing.
    """
    if not isinstance(run, dict):
        return 0
    usage = run.get("usage")
    if _has_structured_usage(usage):
        return core_usage_tokens(usage)
    tokens_used = run.get("tokens_used")
    if isinstance(tokens_used, int) and not isinstance(tokens_used, bool):
        return max(0, int(tokens_used))
    return 0


def _pricing_rate(pricing: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = pricing.get(key)
        if value is not None:
            return float(value)
    return None


def price_usage_tokens(
    pricing: dict[str, Any], usage: dict[str, Any] | None
) -> float | None:
    normalized = normalize_usage_tokens(usage)
    if not pricing or not normalized:
        return None
    input_tokens = normalized["input_tokens"]
    output_tokens = normalized["output_tokens"]
    if input_tokens + output_tokens <= 0:
        return None
    input_rate = _pricing_rate(pricing, "input", "input_per_million_usd")
    output_rate = _pricing_rate(pricing, "output", "output_per_million_usd")
    cached_rate = _pricing_rate(pricing, "cached_input", "cached_input_per_million_usd")
    cache_write_rate = _pricing_rate(
        pricing, "cache_write", "cache_write_input_per_million_usd"
    )
    if input_rate is None or output_rate is None:
        return None
    cached_input_tokens = normalized["cached_input_tokens"]
    cache_write_tokens = int(normalized.get("cache_write_tokens", 0) or 0)
    uncached_input_tokens = max(
        0, input_tokens - cached_input_tokens - cache_write_tokens
    )
    total = (uncached_input_tokens * input_rate) + (output_tokens * output_rate)
    if cached_rate is None:
        total += cached_input_tokens * input_rate
    else:
        total += cached_input_tokens * cached_rate
    if cache_write_rate is None:
        total += cache_write_tokens * input_rate
    else:
        total += cache_write_tokens * cache_write_rate
    return total / 1_000_000
