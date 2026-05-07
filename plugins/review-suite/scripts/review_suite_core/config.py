from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_FILENAME = "default_config.json"
USER_CONFIG_FILENAME = "config.json"
SUPPORTED_REASONING_EFFORTS = {"low", "medium", "high", "xhigh"}
SUPPORTED_SERVICE_TIERS = {"fast", "flex"}


@dataclass(frozen=True)
class LensModelConfig:
    model: str
    reasoning_effort: str
    service_tier: str | None = None


@dataclass(frozen=True)
class GateConfig:
    primary_variant_ids: tuple[str, ...]
    backup_variant_ids: tuple[str, ...]
    default_reviewer_count: int
    initial_reviewer_count: int | None
    max_active_reviewers: int


def plugin_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_config_path() -> Path:
    return plugin_root() / "references" / DEFAULT_CONFIG_FILENAME


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


def load_config(state_dir: Path | None = None) -> dict[str, Any]:
    config = _deep_merge(_read_json(default_config_path()), _read_json(user_config_path(state_dir)))
    _validate_config(config)
    return config


def _string_list(value: Any, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a JSON array of strings")
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


def _optional_positive_int(value: Any, *, field: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, field=field)


def _validate_lens_config(config: dict[str, Any]) -> None:
    lens = config.get("lens")
    if not isinstance(lens, dict):
        raise ValueError("lens config must be an object")
    default = lens.get("default")
    if not isinstance(default, dict):
        raise ValueError("lens.default config must be an object")
    if not str(default.get("model") or "").strip():
        raise ValueError("lens.default.model is required")
    effort = str(default.get("reasoning_effort") or "").strip()
    if effort not in SUPPORTED_REASONING_EFFORTS:
        raise ValueError(f"lens.default.reasoning_effort must be one of: {', '.join(sorted(SUPPORTED_REASONING_EFFORTS))}")
    service_tier = str(default.get("service_tier") or "").strip()
    if service_tier and service_tier not in SUPPORTED_SERVICE_TIERS:
        raise ValueError(f"lens.default.service_tier must be one of: {', '.join(sorted(SUPPORTED_SERVICE_TIERS))}")


def _validate_gate_config(config: dict[str, Any]) -> None:
    gates = config.get("gates")
    if not isinstance(gates, dict):
        raise ValueError("gates config must be an object")
    for gate_name in ("phase_gate", "pr_gate"):
        gate = gates.get(gate_name)
        if not isinstance(gate, dict):
            raise ValueError(f"gates.{gate_name} config must be an object")
        if not _string_list(gate.get("primary_variant_ids"), field=f"gates.{gate_name}.primary_variant_ids"):
            raise ValueError(f"gates.{gate_name}.primary_variant_ids must contain at least one variant id")
        _string_list(gate.get("backup_variant_ids"), field=f"gates.{gate_name}.backup_variant_ids")
        _positive_int(gate.get("default_reviewer_count"), field=f"gates.{gate_name}.default_reviewer_count")
        _optional_positive_int(gate.get("initial_reviewer_count"), field=f"gates.{gate_name}.initial_reviewer_count")
        _positive_int(gate.get("max_active_reviewers"), field=f"gates.{gate_name}.max_active_reviewers")


def _validate_config(config: dict[str, Any]) -> None:
    privacy = config.get("privacy") or {}
    if not isinstance(privacy, dict):
        raise ValueError("privacy config must be an object")
    if bool(privacy.get("arena_external_publish_enabled")):
        raise ValueError("arena_external_publish_enabled is not supported in the public review-suite plugin")
    _validate_lens_config(config)
    _validate_gate_config(config)


def lens_model_config(tool_name: str, *, state_dir: Path | None = None) -> LensModelConfig:
    config = load_config(state_dir)
    lens = dict(config.get("lens") or {})
    merged = _deep_merge(dict(lens.get("default") or {}), dict(lens.get(tool_name) or {}))
    model = str(merged.get("model") or "").strip()
    effort = str(merged.get("reasoning_effort") or "").strip()
    service_tier = str(merged.get("service_tier") or "").strip() or None
    if not model:
        raise ValueError(f"lens model is required for {tool_name}")
    if effort not in SUPPORTED_REASONING_EFFORTS:
        raise ValueError(f"lens reasoning_effort for {tool_name} must be one of: {', '.join(sorted(SUPPORTED_REASONING_EFFORTS))}")
    if service_tier and service_tier not in SUPPORTED_SERVICE_TIERS:
        raise ValueError(f"lens service_tier for {tool_name} must be one of: {', '.join(sorted(SUPPORTED_SERVICE_TIERS))}")
    return LensModelConfig(model=model, reasoning_effort=effort, service_tier=service_tier)


def gate_config(gate_task_class: str, *, state_dir: Path | None = None) -> GateConfig:
    config = load_config(state_dir)
    gate = dict((config.get("gates") or {}).get(gate_task_class) or {})
    if not gate:
        raise ValueError(f"missing gate config for {gate_task_class}")
    return GateConfig(
        primary_variant_ids=_string_list(gate.get("primary_variant_ids"), field=f"gates.{gate_task_class}.primary_variant_ids"),
        backup_variant_ids=_string_list(gate.get("backup_variant_ids"), field=f"gates.{gate_task_class}.backup_variant_ids"),
        default_reviewer_count=_positive_int(
            gate.get("default_reviewer_count"),
            field=f"gates.{gate_task_class}.default_reviewer_count",
        ),
        initial_reviewer_count=_optional_positive_int(
            gate.get("initial_reviewer_count"),
            field=f"gates.{gate_task_class}.initial_reviewer_count",
        ),
        max_active_reviewers=_positive_int(
            gate.get("max_active_reviewers"),
            field=f"gates.{gate_task_class}.max_active_reviewers",
        ),
    )
