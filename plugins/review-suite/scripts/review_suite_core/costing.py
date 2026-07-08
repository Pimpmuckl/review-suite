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
