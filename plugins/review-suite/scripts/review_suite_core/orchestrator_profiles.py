from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SUPPORTED_MODES = ("brief", "normal", "deep", "emergency")
SUPPORTED_SELECTIONS = ("stable", "benchmark", "auto")
SUPPORTED_REASONING_EFFORTS = {"low", "medium", "high", "xhigh"}


@dataclass(frozen=True)
class OrchestratorProfileStep:
    name: str
    count: int
    model: str
    reasoning_effort: str
    service_tier: str | None = None


@dataclass(frozen=True)
class OrchestratorProfile:
    mode: str
    profile: str
    deslop_enabled: bool
    requires_grading: bool
    steps: tuple[OrchestratorProfileStep, ...]


@dataclass(frozen=True)
class OrchestratorProfileResolution:
    requested_mode: str
    effective_mode: str
    requested_selection: str
    effective_selection: str
    profile: OrchestratorProfile

    @property
    def steps(self) -> tuple[OrchestratorProfileStep, ...]:
        return self.profile.steps

    @property
    def requires_grading(self) -> bool:
        return self.profile.requires_grading


def _non_empty_text(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def _positive_int(value: Any, *, field: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if number <= 0:
        raise ValueError(f"{field} must be > 0")
    return number


def _normalize_mode(mode: str) -> str:
    normalized = str(mode or "").strip()
    if normalized not in SUPPORTED_MODES:
        raise ValueError(f"orchestrator mode must be one of: {', '.join(SUPPORTED_MODES)}")
    return normalized


def _normalize_selection(selection: str) -> str:
    normalized = str(selection or "").strip()
    if normalized not in SUPPORTED_SELECTIONS:
        raise ValueError(f"orchestrator selection must be one of: {', '.join(SUPPORTED_SELECTIONS)}")
    return normalized


def _normalize_step(raw_step: Any, *, field: str) -> OrchestratorProfileStep:
    if not isinstance(raw_step, dict):
        raise ValueError(f"{field} must be an object")
    effort = _non_empty_text(raw_step.get("reasoning_effort"), field=f"{field}.reasoning_effort")
    if effort not in SUPPORTED_REASONING_EFFORTS:
        raise ValueError(f"{field}.reasoning_effort must be one of: {', '.join(sorted(SUPPORTED_REASONING_EFFORTS))}")
    service_tier = str(raw_step.get("service_tier") or "").strip() or None
    return OrchestratorProfileStep(
        name=_non_empty_text(raw_step.get("name"), field=f"{field}.name"),
        count=_positive_int(raw_step.get("count"), field=f"{field}.count"),
        model=_non_empty_text(raw_step.get("model"), field=f"{field}.model"),
        reasoning_effort=effort,
        service_tier=service_tier,
    )


def _normalize_profile(raw_profile: Any, *, mode: str, profile: str) -> OrchestratorProfile:
    if not isinstance(raw_profile, dict):
        raise ValueError(f"orchestrator.profiles.{profile}.{mode} must be an object")
    raw_steps = raw_profile.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError(f"orchestrator.profiles.{profile}.{mode}.steps must contain at least one step")
    steps = tuple(
        _normalize_step(raw_step, field=f"orchestrator.profiles.{profile}.{mode}.steps[{index}]")
        for index, raw_step in enumerate(raw_steps)
    )
    requires_grading = profile == "benchmark"
    if "requires_grading" in raw_profile and bool(raw_profile.get("requires_grading")) is not requires_grading:
        raise ValueError(f"orchestrator.profiles.{profile}.{mode}.requires_grading must be {str(requires_grading).lower()}")
    return OrchestratorProfile(
        mode=mode,
        profile=profile,
        deslop_enabled=bool(raw_profile.get("deslop_enabled", True)),
        requires_grading=requires_grading,
        steps=steps,
    )


def load_orchestrator_profiles(config: dict[str, Any]) -> dict[str, dict[str, OrchestratorProfile]]:
    orchestrator = config.get("orchestrator") or {}
    if not isinstance(orchestrator, dict):
        raise ValueError("orchestrator config must be an object")
    raw_profiles = orchestrator.get("profiles") or {}
    if not isinstance(raw_profiles, dict):
        raise ValueError("orchestrator.profiles config must be an object")

    normalized: dict[str, dict[str, OrchestratorProfile]] = {}
    for profile in ("stable", "benchmark"):
        raw_modes = raw_profiles.get(profile) or {}
        if not isinstance(raw_modes, dict):
            raise ValueError(f"orchestrator.profiles.{profile} must be an object")
        normalized[profile] = {
            mode: _normalize_profile(raw_modes[mode], mode=mode, profile=profile)
            for mode in SUPPORTED_MODES
            if mode in raw_modes
        }
    return normalized


def resolve_orchestrator_profile(
    config: dict[str, Any],
    *,
    mode: str,
    selection: str = "auto",
) -> OrchestratorProfileResolution:
    requested_mode = _normalize_mode(mode)
    requested_selection = _normalize_selection(selection)
    profiles = load_orchestrator_profiles(config)

    effective_selection = requested_selection
    if effective_selection == "auto":
        effective_selection = "stable" if requested_mode in profiles["stable"] else "benchmark"

    profile = profiles[effective_selection].get(requested_mode)
    if profile is None:
        raise ValueError(f"missing orchestrator {effective_selection} profile for mode {requested_mode}")
    return OrchestratorProfileResolution(
        requested_mode=requested_mode,
        effective_mode=profile.mode,
        requested_selection=requested_selection,
        effective_selection=effective_selection,
        profile=profile,
    )
