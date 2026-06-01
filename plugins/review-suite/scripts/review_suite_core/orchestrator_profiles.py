from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SUPPORTED_MODES = ("brief", "normal", "deep", "emergency")
SUPPORTED_SELECTIONS = ("stable", "benchmark", "auto")
RESTART_MODE_ORDER = {"brief": 0, "normal": 1, "deep": 2}
SUPPORTED_SELECTION_REASONS = (
    "explicit_stable",
    "explicit_benchmark",
    "auto_stable_profile",
    "auto_benchmark_missing_stable",
)
SUPPORTED_REASONING_EFFORTS = {"low", "medium", "high", "xhigh"}
SUPPORTED_SERVICE_TIERS = {"fast", "flex"}
SUPPORTED_STEP_KINDS = ("review", "gate", "arena")
SUPPORTED_GATE_TASK_CLASSES = ("phase_gate", "pr_gate")
SUPPORTED_ARENA_LANES = ("review_t1", "review_t3")
SUPPORTED_ARENA_TASK_CLASSES = ("phase_review", "pr_review")
ARENA_LANES_BY_TASK_CLASS = {
    "phase_review": "review_t1",
    "pr_review": "review_t3",
}


@dataclass(frozen=True)
class OrchestratorProfileStep:
    kind: str
    name: str
    count: int | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    service_tier: str | None = None
    gate: str | None = None
    lane: str | None = None
    task_class: str | None = None
    rerun_on_findings: bool = False


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
    selection_reason: str
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


def _non_negative_int(value: Any, *, field: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if number < 0:
        raise ValueError(f"{field} must be >= 0")
    return number


def _optional_bool(value: Any, *, field: str) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be true or false")
    return value


def _parse_model_label(value: Any, *, field: str) -> tuple[str, str, str | None]:
    label = _non_empty_text(value, field=field)
    with_tier = label.rsplit("-", 2)
    if len(with_tier) == 3 and with_tier[1] in SUPPORTED_REASONING_EFFORTS and with_tier[2] in SUPPORTED_SERVICE_TIERS:
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
    efforts = ", ".join(sorted(SUPPORTED_REASONING_EFFORTS))
    raise ValueError(f"{field} must look like model-effort where effort is one of: {efforts}")


def _model_from_step(
    raw_step: dict[str, Any],
    *,
    stable_defaults: dict[str, Any],
    field: str,
) -> tuple[str, str, str | None]:
    model_ref = str(raw_step.get("model_ref") or "").strip()
    if model_ref:
        if "model" in raw_step or "reasoning_effort" in raw_step:
            raise ValueError(f"{field} must use either model_ref or model/reasoning_effort")
        return _parse_model_label(
            stable_defaults.get(model_ref),
            field=f"orchestrator.stable_defaults.{model_ref}",
        )
    model = _non_empty_text(raw_step.get("model"), field=f"{field}.model")
    effort = _non_empty_text(raw_step.get("reasoning_effort"), field=f"{field}.reasoning_effort")
    if effort not in SUPPORTED_REASONING_EFFORTS:
        raise ValueError(f"{field}.reasoning_effort must be one of: {', '.join(sorted(SUPPORTED_REASONING_EFFORTS))}")
    return model, effort, None


def _service_tier(raw_step: dict[str, Any], *, default: str | None, field: str) -> str | None:
    service_tier = str(raw_step.get("service_tier") or default or "").strip() or None
    if service_tier and service_tier not in SUPPORTED_SERVICE_TIERS:
        raise ValueError(f"{field}.service_tier must be one of: {', '.join(sorted(SUPPORTED_SERVICE_TIERS))}")
    return service_tier


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


def _normalize_step_kind(value: Any, *, field: str) -> str:
    kind = str(value or "review").strip() or "review"
    if kind not in SUPPORTED_STEP_KINDS:
        raise ValueError(f"{field}.kind must be one of: {', '.join(SUPPORTED_STEP_KINDS)}")
    return kind


def _normalize_gate(value: Any, *, field: str) -> str:
    gate = _non_empty_text(value, field=f"{field}.gate")
    if gate not in SUPPORTED_GATE_TASK_CLASSES:
        raise ValueError(f"{field}.gate must be one of: {', '.join(SUPPORTED_GATE_TASK_CLASSES)}")
    return gate


def _normalize_arena_lane(value: Any, *, field: str) -> str:
    lane = _non_empty_text(value, field=f"{field}.lane")
    if lane not in SUPPORTED_ARENA_LANES:
        raise ValueError(f"{field}.lane must be one of: {', '.join(SUPPORTED_ARENA_LANES)}")
    return lane


def _normalize_arena_task_class(value: Any, *, field: str) -> str:
    task_class = _non_empty_text(value, field=f"{field}.task_class")
    if task_class not in SUPPORTED_ARENA_TASK_CLASSES:
        raise ValueError(f"{field}.task_class must be one of: {', '.join(SUPPORTED_ARENA_TASK_CLASSES)}")
    return task_class


def _normalize_arena_pair(raw_step: dict[str, Any], *, field: str) -> tuple[str, str]:
    lane = _normalize_arena_lane(raw_step.get("lane"), field=field)
    task_class = _normalize_arena_task_class(raw_step.get("task_class"), field=field)
    expected_lane = ARENA_LANES_BY_TASK_CLASS[task_class]
    if lane != expected_lane:
        raise ValueError(f"{field}.lane must be {expected_lane} for task_class {task_class}")
    return lane, task_class


def _raw_loop_ref(raw_step: Any) -> str:
    if not isinstance(raw_step, dict):
        return ""
    return str(raw_step.get("loop_ref") or "").strip()


def _config_ref(config: dict[str, Any], ref: str, *, field: str) -> Any:
    current: Any = config
    for part in ref.split("."):
        key = part.strip()
        if not key or not isinstance(current, dict) or key not in current:
            raise ValueError(f"{field} references unknown config value {ref}")
        current = current[key]
    return current


def _config_bool_ref(config: dict[str, Any], ref: str, *, field: str) -> bool:
    value = _config_ref(config, ref, field=field)
    if not isinstance(value, bool):
        raise ValueError(f"{field} must reference a true or false config value")
    return value


def _step_enabled(raw_step: dict[str, Any], *, config: dict[str, Any], field: str) -> bool:
    enabled_ref = str(raw_step.get("enabled_ref") or "").strip()
    if not enabled_ref:
        return True
    return _config_bool_ref(config, enabled_ref, field=f"{field}.enabled_ref")


def _loop_count(
    raw_step: dict[str, Any],
    *,
    config: dict[str, Any],
    stable_defaults: dict[str, Any],
    loop_ref: str,
    field: str,
) -> int:
    if not _step_enabled(raw_step, config=config, field=field):
        return 0
    allow_zero = _normalize_step_kind(raw_step.get("kind"), field=field) == "arena" or "enabled_ref" in raw_step
    if allow_zero:
        loops = _non_negative_int(stable_defaults.get(loop_ref), field=f"orchestrator.stable_defaults.{loop_ref}")
    else:
        loops = _positive_int(stable_defaults.get(loop_ref), field=f"orchestrator.stable_defaults.{loop_ref}")

    subtract_ref = str(raw_step.get("subtract_loop_ref") or "").strip()
    if subtract_ref:
        subtract_if = str(raw_step.get("subtract_if") or "").strip()
        if not subtract_if or _config_bool_ref(config, subtract_if, field=f"{field}.subtract_if"):
            loops -= _non_negative_int(
                stable_defaults.get(subtract_ref),
                field=f"orchestrator.stable_defaults.{subtract_ref}",
            )
        min_loops = _positive_int(raw_step.get("min_loops", 1), field=f"{field}.min_loops")
        loops = max(min_loops, loops)
    return loops


def _normalize_step(
    raw_step: Any,
    *,
    field: str,
    config: dict[str, Any],
    stable_defaults: dict[str, Any],
    name_suffix: str = "",
) -> OrchestratorProfileStep:
    if not isinstance(raw_step, dict):
        raise ValueError(f"{field} must be an object")
    kind = _normalize_step_kind(raw_step.get("kind"), field=field)
    name = f"{_non_empty_text(raw_step.get('name'), field=f'{field}.name')}{name_suffix}"
    if kind == "gate":
        return OrchestratorProfileStep(
            kind=kind,
            name=name,
            gate=_normalize_gate(raw_step.get("gate"), field=field),
        )
    if kind == "arena":
        lane, task_class = _normalize_arena_pair(raw_step, field=field)
        return OrchestratorProfileStep(
            kind=kind,
            name=name,
            lane=lane,
            task_class=task_class,
        )
    model, effort, default_service_tier = _model_from_step(raw_step, stable_defaults=stable_defaults, field=field)
    return OrchestratorProfileStep(
        kind=kind,
        name=name,
        count=_positive_int(raw_step.get("count"), field=f"{field}.count"),
        model=model,
        reasoning_effort=effort,
        service_tier=_service_tier(raw_step, default=default_service_tier, field=field),
        rerun_on_findings=_optional_bool(raw_step.get("rerun_on_findings"), field=f"{field}.rerun_on_findings"),
    )


def _normalize_steps(
    raw_steps: list[Any],
    *,
    field: str,
    config: dict[str, Any],
    stable_defaults: dict[str, Any],
) -> tuple[OrchestratorProfileStep, ...]:
    steps: list[OrchestratorProfileStep] = []
    index = 0
    while index < len(raw_steps):
        loop_ref = _raw_loop_ref(raw_steps[index])
        if not loop_ref:
            if isinstance(raw_steps[index], dict) and not _step_enabled(raw_steps[index], config=config, field=f"{field}[{index}]"):
                index += 1
                continue
            steps.append(
                _normalize_step(
                    raw_steps[index],
                    field=f"{field}[{index}]",
                    config=config,
                    stable_defaults=stable_defaults,
                )
            )
            index += 1
            continue
        block: list[tuple[int, Any]] = []
        while index < len(raw_steps) and _raw_loop_ref(raw_steps[index]) == loop_ref:
            block.append((index, raw_steps[index]))
            index += 1
        enabled_block = [
            (raw_index, raw_step)
            for raw_index, raw_step in block
            if not isinstance(raw_step, dict) or _step_enabled(raw_step, config=config, field=f"{field}[{raw_index}]")
        ]
        if not enabled_block:
            continue
        loops = _loop_count(
            enabled_block[0][1],
            config=config,
            stable_defaults=stable_defaults,
            loop_ref=loop_ref,
            field=f"{field}[{enabled_block[0][0]}]",
        )
        for loop_index in range(loops):
            suffix = f"-{loop_index + 1}" if loops > 1 else ""
            for raw_index, raw_step in enabled_block:
                steps.append(
                    _normalize_step(
                        raw_step,
                        field=f"{field}[{raw_index}]",
                        config=config,
                        stable_defaults=stable_defaults,
                        name_suffix=suffix,
                    )
                )
    return tuple(steps)


def _normalize_profile(
    raw_profile: Any,
    *,
    mode: str,
    profile: str,
    config: dict[str, Any],
    stable_defaults: dict[str, Any],
) -> OrchestratorProfile:
    if not isinstance(raw_profile, dict):
        raise ValueError(f"orchestrator.profiles.{profile}.{mode} must be an object")
    raw_steps = raw_profile.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError(f"orchestrator.profiles.{profile}.{mode}.steps must contain at least one step")
    steps = _normalize_steps(
        raw_steps,
        field=f"orchestrator.profiles.{profile}.{mode}.steps",
        config=config,
        stable_defaults=stable_defaults,
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


def _validate_calibration_policy(orchestrator: dict[str, Any]) -> None:
    raw_calibration = orchestrator.get("calibration") or {}
    if not isinstance(raw_calibration, dict):
        raise ValueError("orchestrator.calibration must be an object")
    if bool(raw_calibration.get("auto_promotion_enabled", False)):
        raise ValueError("orchestrator.calibration.auto_promotion_enabled must be false")


def _stable_defaults(orchestrator: dict[str, Any]) -> dict[str, Any]:
    raw_defaults = orchestrator.get("stable_defaults") or {}
    if not isinstance(raw_defaults, dict):
        raise ValueError("orchestrator.stable_defaults must be an object")
    return raw_defaults


def load_orchestrator_profiles(config: dict[str, Any]) -> dict[str, dict[str, OrchestratorProfile]]:
    orchestrator = config.get("orchestrator") or {}
    if not isinstance(orchestrator, dict):
        raise ValueError("orchestrator config must be an object")
    _validate_calibration_policy(orchestrator)
    stable_defaults = _stable_defaults(orchestrator)
    raw_profiles = orchestrator.get("profiles") or {}
    if not isinstance(raw_profiles, dict):
        raise ValueError("orchestrator.profiles config must be an object")

    normalized: dict[str, dict[str, OrchestratorProfile]] = {}
    for profile in ("stable", "benchmark"):
        raw_modes = raw_profiles.get(profile) or {}
        if not isinstance(raw_modes, dict):
            raise ValueError(f"orchestrator.profiles.{profile} must be an object")
        normalized[profile] = {
            mode: _normalize_profile(
                raw_modes[mode],
                mode=mode,
                profile=profile,
                config=config,
                stable_defaults=stable_defaults if profile == "stable" else {},
            )
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
    selection_reason = f"explicit_{requested_selection}"
    if requested_selection == "auto":
        if requested_mode in profiles["stable"]:
            effective_selection = "stable"
            selection_reason = "auto_stable_profile"
        else:
            effective_selection = "benchmark"
            selection_reason = "auto_benchmark_missing_stable"

    profile = profiles[effective_selection].get(requested_mode)
    if profile is None:
        raise ValueError(f"missing orchestrator {effective_selection} profile for mode {requested_mode}")
    return OrchestratorProfileResolution(
        requested_mode=requested_mode,
        effective_mode=profile.mode,
        requested_selection=requested_selection,
        effective_selection=effective_selection,
        selection_reason=selection_reason,
        profile=profile,
    )
