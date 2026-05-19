from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from review_suite_core.config import load_config
from review_suite_core.orchestrator_profiles import load_orchestrator_profiles, resolve_orchestrator_profile


def test_default_stable_profiles_cover_all_modes(tmp_path: Path) -> None:
    config = load_config(tmp_path / "state")

    profiles = load_orchestrator_profiles(config)

    assert set(profiles["stable"]) == {"brief", "normal", "deep", "emergency"}
    assert profiles["stable"]["brief"].steps[0].count == 2
    assert profiles["stable"]["brief"].steps[0].model == "gpt-5.5"
    assert [step.model for step in profiles["stable"]["normal"].steps if step.kind == "review"] == ["gpt-5.4", "gpt-5.5"]
    assert [step.count for step in profiles["stable"]["normal"].steps if step.kind == "review"] == [4, 2]
    assert {step.reasoning_effort for step in profiles["stable"]["deep"].steps if step.kind == "review"} == {"xhigh"}
    for mode in ("brief", "normal", "deep", "emergency"):
        step = profiles["stable"][mode].steps[-1]
        assert step.kind == "gate"
        assert step.gate == "phase_gate"
    assert profiles["stable"]["emergency"].deslop_enabled is False


def test_profile_step_kind_defaults_to_review(tmp_path: Path) -> None:
    config = deepcopy(load_config(tmp_path / "state"))
    config["orchestrator"]["profiles"]["stable"]["brief"]["steps"] = [
        {"name": "precision", "count": 1, "model": "gpt-5.5", "reasoning_effort": "medium"}
    ]

    profiles = load_orchestrator_profiles(config)

    assert profiles["stable"]["brief"].steps[0].kind == "review"


@pytest.mark.parametrize("mode", ["brief", "normal", "deep", "emergency"])
def test_auto_selection_uses_stable_profile_when_configured(tmp_path: Path, mode: str) -> None:
    config = load_config(tmp_path / "state")

    resolution = resolve_orchestrator_profile(config, mode=mode, selection="auto")

    assert resolution.requested_mode == mode
    assert resolution.effective_mode == mode
    assert resolution.requested_selection == "auto"
    assert resolution.effective_selection == "stable"
    assert resolution.requires_grading is False
    assert resolution.steps


def test_benchmark_selection_requires_grading(tmp_path: Path) -> None:
    config = load_config(tmp_path / "state")

    resolution = resolve_orchestrator_profile(config, mode="normal", selection="benchmark")

    assert resolution.effective_selection == "benchmark"
    assert resolution.requires_grading is True
    assert resolution.profile.profile == "benchmark"


def test_auto_selection_falls_back_to_benchmark_when_stable_mode_missing(tmp_path: Path) -> None:
    config = deepcopy(load_config(tmp_path / "state"))
    del config["orchestrator"]["profiles"]["stable"]["normal"]

    resolution = resolve_orchestrator_profile(config, mode="normal", selection="auto")

    assert resolution.effective_selection == "benchmark"
    assert resolution.requires_grading is True
    assert resolution.steps[0].name == "benchmark"


def test_explicit_stable_selection_requires_stable_profile(tmp_path: Path) -> None:
    config = deepcopy(load_config(tmp_path / "state"))
    del config["orchestrator"]["profiles"]["stable"]["normal"]

    with pytest.raises(ValueError, match="missing orchestrator stable profile for mode normal"):
        resolve_orchestrator_profile(config, mode="normal", selection="stable")
