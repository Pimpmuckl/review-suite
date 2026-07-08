from __future__ import annotations

from typing import Any

SUPPORTED_REASONING_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
SUPPORTED_SERVICE_TIERS = {"fast", "flex"}
DEEP_REASONING_EFFORTS = {"high", "xhigh", "max"}
GPT56_MODELS = {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}
MODEL_REASONING_EFFORT_FALLBACKS = {
    "gpt-5.5": {"low", "medium", "high", "xhigh"},
    "gpt-5.4": {"low", "medium", "high", "xhigh"},
    "gpt-5.4-mini": {"medium", "high", "xhigh"},
}


def supported_reasoning_efforts_text() -> str:
    return ", ".join(sorted(SUPPORTED_REASONING_EFFORTS))


def parse_model_label(value: Any, *, field: str) -> tuple[str, str, str | None]:
    label = str(value or "").strip()
    if not label:
        raise ValueError(f"{field} is required")
    with_tier = label.rsplit("-", 2)
    if (
        len(with_tier) == 3
        and with_tier[1] in SUPPORTED_REASONING_EFFORTS
        and with_tier[2] in SUPPORTED_SERVICE_TIERS
    ):
        model = with_tier[0].strip()
        if not model:
            raise ValueError(f"{field} must include a model name")
        return model, with_tier[1], with_tier[2]
    without_tier = label.rsplit("-", 1)
    if len(without_tier) == 2 and without_tier[1] in SUPPORTED_REASONING_EFFORTS:
        model = without_tier[0].strip()
        if not model:
            raise ValueError(f"{field} must include a model name")
        return model, without_tier[1], None
    raise ValueError(
        f"{field} must look like model-effort where effort is one of: {supported_reasoning_efforts_text()}"
    )


def is_deep_reasoning_effort(value: str | None) -> bool:
    return str(value or "").strip().lower() in DEEP_REASONING_EFFORTS


def codex_reasoning_effort(model: str, effort: str) -> str:
    requested = str(effort or "").strip().lower()
    if not requested:
        return requested
    model_name = str(model or "").strip().lower()
    if model_name in GPT56_MODELS:
        if requested == "minimal":
            return "low"
        return requested
    supported = MODEL_REASONING_EFFORT_FALLBACKS.get(model_name)
    if supported is None:
        return requested
    if requested == "max" and "xhigh" in supported:
        return "xhigh"
    if requested == "xhigh" and "xhigh" not in supported and "high" in supported:
        return "high"
    return requested
