from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from review_suite_core.config import gate_config, lens_model_config, load_config


def test_default_public_config_loads(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    config = load_config(state_dir)

    assert config["privacy"]["arena_external_publish_enabled"] is False
    assert lens_model_config("review-plan", state_dir=state_dir).model == "gpt-5.5"
    assert lens_model_config("review-plan", state_dir=state_dir).reasoning_effort == "medium"
    assert gate_config("phase_gate", state_dir=state_dir).primary_variant_ids == ("gpt-5.4-medium",)
    assert gate_config("pr_gate", state_dir=state_dir).primary_variant_ids == ("gpt-5.5-xhigh",)


def test_user_config_overrides_defaults(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "config.json").write_text(
        json.dumps(
            {
                "lens": {
                    "review-plan": {
                        "model": "gpt-5.4",
                        "reasoning_effort": "high",
                    }
                },
                "gates": {
                    "phase_gate": {
                        "primary_variant_ids": ["custom-phase"],
                        "default_reviewer_count": 3,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    lens = lens_model_config("review-plan", state_dir=state_dir)
    phase_gate = gate_config("phase_gate", state_dir=state_dir)
    pr_gate = gate_config("pr_gate", state_dir=state_dir)

    assert lens.model == "gpt-5.4"
    assert lens.reasoning_effort == "high"
    assert phase_gate.primary_variant_ids == ("custom-phase",)
    assert phase_gate.default_reviewer_count == 3
    assert pr_gate.primary_variant_ids == ("gpt-5.5-xhigh",)


def test_public_config_rejects_external_arena_publish(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "config.json").write_text(
        json.dumps({"privacy": {"arena_external_publish_enabled": True}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="arena_external_publish_enabled"):
        load_config(state_dir)


def test_no_external_arena_publish_endpoint_in_scripts() -> None:
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    banned = (
        "arena" + "-web",
        "review" + "arena",
        "review" + "-arena",
        "arena." + "private-host",
        "review" + "arena.",
    )
    hits: list[str] = []
    for path in scripts_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        for marker in banned:
            if marker in text:
                hits.append(f"{path.name}:{marker}")

    assert hits == []
