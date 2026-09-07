from __future__ import annotations

import json
import os
import tempfile
import tomllib
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model_labels import (
    SUPPORTED_REASONING_EFFORTS,
    SUPPORTED_SERVICE_TIERS,
    parse_model_label,
    supported_reasoning_efforts_text,
)


DEFAULT_CONFIG_FILENAME = "default_settings.toml"
USER_CONFIG_FILENAME = "settings.toml"
SETTINGS_STUB = (
    "# Optional overrides only. Omitted settings follow the installed defaults.\n"
)
SUPPORTED_ORCHESTRATOR_SELECTIONS = {"auto", "stable"}


@dataclass(frozen=True)
class LensModelConfig:
    model: str
    reasoning_effort: str
    service_tier: str | None = None


@dataclass(frozen=True)
class GateConfig:
    discovery_variant_id: str
    discovery_reviewer_count: int
    signoff_variant_id: str
    signoff_reviewer_count: int
    discovery_loops: int
    backup_variant_ids: tuple[str, ...]
    max_active_reviewers: int


def plugin_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_config_path() -> Path:
    return plugin_root() / DEFAULT_CONFIG_FILENAME


def default_state_dir() -> Path:
    return Path.home() / ".codex" / "state" / "review-suite"


def user_config_path(state_dir: Path | None = None) -> Path:
    return (state_dir or default_state_dir()) / USER_CONFIG_FILENAME


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid review-suite config JSON at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"review-suite config must be a JSON object: {path}")
    return payload


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(
            f"invalid review-suite settings TOML at {path}: {exc}"
        ) from exc


def _toml_value(value: Any) -> str:
    # Migration writes JSON-compatible values as TOML inline tables/arrays.
    if isinstance(value, dict):
        return (
            "{ "
            + ", ".join(
                f"{json.dumps(key, ensure_ascii=False)} = {_toml_value(item)}"
                for key, item in value.items()
                if item is not None
            )
            + " }"
        )
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if value is None:
        raise ValueError("cannot migrate a null array item to TOML")
    return json.dumps(value, ensure_ascii=False)


def _toml_settings(settings: dict[str, Any], path: tuple[str, ...] = ()) -> str:
    values = [
        f"{json.dumps(key, ensure_ascii=False)} = {_toml_value(value)}\n"
        for key, value in settings.items()
        if value is not None and not isinstance(value, dict)
    ]
    header = "[" + ".".join(json.dumps(key, ensure_ascii=False) for key in path) + "]\n"
    return (
        (header if path and values else "")
        + "".join(values)
        + "".join(
            "\n" + _toml_settings(value, (*path, key))
            for key, value in settings.items()
            if isinstance(value, dict) and value
        )
    )


def _non_model_settings(value: Any) -> Any:
    if isinstance(value, list):
        return [_non_model_settings(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: _non_model_settings(item)
        for key, item in value.items()
        if key
        not in {
            "model",
            "reasoning",
            "reasoning_effort",
            "model_ref",
            "variant_ids",
            "variant_groups",
            "backup_variant_ids",
        }
        and not key.endswith(("_model", "_model_ref"))
    }


def _migrate_settings(legacy: dict[str, Any]) -> dict[str, Any]:
    migrated = _non_model_settings(legacy)
    lens = migrated.pop("lens", {})
    for tool, settings in lens.items():
        if settings:
            if tool == "default":
                migrated["normal"] = settings
            else:
                migrated.setdefault("jobs", {})[tool.removeprefix("review-")] = settings
    for profile in migrated.get("orchestrator", {}).get("profiles", {}).values():
        for mode in profile.values():
            for step in mode.get("steps", []):
                if step.get("kind", "review") == "review":
                    step["model_ref"] = (
                        "signoff_deep_model"
                        if step.get("name") == "deep-signoff"
                        else "signoff_normal_model"
                    )
    return migrated


def _create_user_settings(path: Path, defaults: dict[str, Any]) -> None:
    legacy = _read_json(path.with_name("config.json"))
    overrides = _migrate_settings(legacy)
    _resolved_config(_deep_merge(defaults, overrides))
    content = SETTINGS_STUB + _toml_settings(overrides)
    # Concurrent launchers must never see a partial file or overwrite user edits.
    tomllib.loads(content)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(content)
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        temporary.unlink()


def _job_model(config: dict[str, Any], job: str) -> dict[str, Any]:
    group = "deep" if job == "pr_discovery" else "normal"
    base = config.get(group)
    jobs = config.get("jobs", {})
    if not isinstance(base, dict):
        raise ValueError(f"{group} must be a TOML table")
    if not isinstance(jobs, dict) or not isinstance(jobs.get(job, {}), dict):
        raise ValueError(f"jobs.{job} must be a TOML table")
    settings = _deep_merge(base, jobs.get(job, {}))
    model = _non_empty_text(settings.get("model"), field=f"jobs.{job}.model")
    effort = settings.get("reasoning")
    if effort not in SUPPORTED_REASONING_EFFORTS:
        raise ValueError(
            f"jobs.{job}.reasoning must be one of: {supported_reasoning_efforts_text()}"
        )
    tier = settings.get("service_tier") or None
    if tier and tier not in SUPPORTED_SERVICE_TIERS:
        raise ValueError(
            f"jobs.{job}.service_tier must be one of: {', '.join(sorted(SUPPORTED_SERVICE_TIERS))}"
        )
    return {"model": model, "reasoning_effort": effort, "service_tier": tier}


def _resolved_config(config: dict[str, Any]) -> dict[str, Any]:
    # Keep the existing workflow representation internal; model policy lives above it.
    for job in ("plan", "deslop", "followup"):
        _job_model(config, job)
    defaults = config["orchestrator"]["stable_defaults"]
    for ref, job in {
        "discovery_phase_model": "phase_discovery",
        "discovery_deep_model": "pr_discovery",
        "signoff_normal_model": "normal_signoff",
        "signoff_deep_model": "deep_signoff",
    }.items():
        model = _job_model(config, job)
        defaults[ref] = "-".join(str(value) for value in model.values() if value)
    for name, gate in config["gates"].items():
        gate["discovery_model_ref"] = (
            "discovery_deep_model" if name == "pr_gate" else "discovery_phase_model"
        )
        gate["signoff_model_ref"] = (
            "signoff_deep_model" if name == "pr_gate" else "signoff_normal_model"
        )
    _validate_config(config)
    return config


def load_config(state_dir: Path | None = None) -> dict[str, Any]:
    defaults = _read_toml(default_config_path())
    path = user_config_path(state_dir)
    if not path.exists():
        _create_user_settings(path, defaults)
    return _resolved_config(_deep_merge(defaults, _read_toml(path)))


def _string_list(value: Any, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array of strings")
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            raise ValueError(f"{field} must not contain empty values")
        result.append(text)
    return tuple(result)


def _positive_int(value: Any, *, field: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if number <= 0:
        raise ValueError(f"{field} must be > 0")
    return number


def _non_empty_text(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def _parse_model_label(value: Any, *, field: str) -> str:
    model, effort, service_tier = parse_model_label(value, field=field)
    return "-".join(part for part in (model, effort, service_tier) if part)


def _orchestrator_defaults(config: dict[str, Any]) -> dict[str, Any]:
    orchestrator = config.get("orchestrator")
    if not isinstance(orchestrator, dict):
        raise ValueError("orchestrator config must be an object")
    defaults = orchestrator.get("stable_defaults") or {}
    if not isinstance(defaults, dict):
        raise ValueError("orchestrator.stable_defaults must be an object")
    return defaults


def _stable_model_ref(config: dict[str, Any], ref: Any, *, field: str) -> str:
    ref_name = _non_empty_text(ref, field=field)
    return _parse_model_label(
        _orchestrator_defaults(config).get(ref_name),
        field=f"orchestrator.stable_defaults.{ref_name}",
    )


def _stable_positive_int_ref(config: dict[str, Any], ref: Any, *, field: str) -> int:
    ref_name = _non_empty_text(ref, field=field)
    return _positive_int(
        _orchestrator_defaults(config).get(ref_name),
        field=f"orchestrator.stable_defaults.{ref_name}",
    )


def _validate_gate_config(config: dict[str, Any]) -> None:
    gates = config.get("gates")
    if not isinstance(gates, dict):
        raise ValueError("gates config must be an object")
    for gate_name in ("phase_gate", "pr_gate"):
        gate = gates.get(gate_name)
        if not isinstance(gate, dict):
            raise ValueError(f"gates.{gate_name} config must be an object")
        _stable_model_ref(
            config,
            gate.get("discovery_model_ref"),
            field=f"gates.{gate_name}.discovery_model_ref",
        )
        _positive_int(
            gate.get("discovery_reviewer_count"),
            field=f"gates.{gate_name}.discovery_reviewer_count",
        )
        _stable_model_ref(
            config,
            gate.get("signoff_model_ref"),
            field=f"gates.{gate_name}.signoff_model_ref",
        )
        _positive_int(
            gate.get("signoff_reviewer_count"),
            field=f"gates.{gate_name}.signoff_reviewer_count",
        )
        _stable_positive_int_ref(
            config,
            gate.get("discovery_loops_ref"),
            field=f"gates.{gate_name}.discovery_loops_ref",
        )
        _string_list(
            gate.get("backup_variant_ids"),
            field=f"gates.{gate_name}.backup_variant_ids",
        )
        _positive_int(
            gate.get("max_active_reviewers"),
            field=f"gates.{gate_name}.max_active_reviewers",
        )


def _validate_orchestrator_config(config: dict[str, Any]) -> None:
    orchestrator = config.get("orchestrator")
    if not isinstance(orchestrator, dict):
        raise ValueError("orchestrator config must be an object")
    selection = str(orchestrator.get("selection") or "auto").strip()
    if selection not in SUPPORTED_ORCHESTRATOR_SELECTIONS:
        allowed = ", ".join(sorted(SUPPORTED_ORCHESTRATOR_SELECTIONS))
        raise ValueError(f"orchestrator.selection must be one of: {allowed}")
    _orchestrator_defaults(config)


def _validate_config(config: dict[str, Any]) -> None:
    privacy = config.get("privacy") or {}
    if not isinstance(privacy, dict):
        raise ValueError("privacy config must be an object")
    if bool(privacy.get("arena_external_publish_enabled")):
        raise ValueError(
            "arena_external_publish_enabled is not supported in the public review-suite plugin"
        )
    _validate_gate_config(config)
    _validate_orchestrator_config(config)


def lens_model_config(
    tool_name: str, *, state_dir: Path | None = None
) -> LensModelConfig:
    return LensModelConfig(
        **_job_model(load_config(state_dir), tool_name.removeprefix("review-"))
    )


def gate_config(gate_task_class: str, *, state_dir: Path | None = None) -> GateConfig:
    config = load_config(state_dir)
    gate = dict((config.get("gates") or {}).get(gate_task_class) or {})
    if not gate:
        raise ValueError(f"missing gate config for {gate_task_class}")
    return GateConfig(
        discovery_variant_id=_stable_model_ref(
            config,
            gate.get("discovery_model_ref"),
            field=f"gates.{gate_task_class}.discovery_model_ref",
        ),
        discovery_reviewer_count=_positive_int(
            gate.get("discovery_reviewer_count"),
            field=f"gates.{gate_task_class}.discovery_reviewer_count",
        ),
        signoff_variant_id=_stable_model_ref(
            config,
            gate.get("signoff_model_ref"),
            field=f"gates.{gate_task_class}.signoff_model_ref",
        ),
        signoff_reviewer_count=_positive_int(
            gate.get("signoff_reviewer_count"),
            field=f"gates.{gate_task_class}.signoff_reviewer_count",
        ),
        discovery_loops=_stable_positive_int_ref(
            config,
            gate.get("discovery_loops_ref"),
            field=f"gates.{gate_task_class}.discovery_loops_ref",
        ),
        backup_variant_ids=_string_list(
            gate.get("backup_variant_ids"),
            field=f"gates.{gate_task_class}.backup_variant_ids",
        ),
        max_active_reviewers=_positive_int(
            gate.get("max_active_reviewers"),
            field=f"gates.{gate_task_class}.max_active_reviewers",
        ),
    )
