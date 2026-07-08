from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from review_suite_core.config import gate_config, lens_model_config, load_config
from review_suite_core.model_labels import parse_model_label


def test_default_public_config_loads(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    config = load_config(state_dir)

    assert config["privacy"]["arena_external_publish_enabled"] is False
    assert config["arena"]["enabled"] is False
    assert lens_model_config("review-plan", state_dir=state_dir).model == "gpt-5.5"
    assert (
        lens_model_config("review-plan", state_dir=state_dir).reasoning_effort
        == "medium"
    )
    assert (
        gate_config("phase_gate", state_dir=state_dir).discovery_variant_id
        == "gpt-5.4-medium"
    )
    assert gate_config("phase_gate", state_dir=state_dir).discovery_reviewer_count == 4
    assert (
        gate_config("phase_gate", state_dir=state_dir).signoff_variant_id
        == "gpt-5.5-medium"
    )
    assert gate_config("phase_gate", state_dir=state_dir).signoff_reviewer_count == 2
    assert gate_config("phase_gate", state_dir=state_dir).discovery_loops == 1
    assert (
        gate_config("pr_gate", state_dir=state_dir).discovery_variant_id
        == "gpt-5.4-xhigh"
    )
    assert (
        gate_config("pr_gate", state_dir=state_dir).signoff_variant_id
        == "gpt-5.5-xhigh"
    )
    assert config["orchestrator"]["selection"] == "auto"
    assert config["orchestrator"]["stable_defaults"] == {
        "discovery_brief_model": "gpt-5.4-medium",
        "discovery_deep_model": "gpt-5.4-xhigh",
        "discovery_loops": 1,
        "normal_discovery_loops": 3,
        "normal_arena_loops": 0,
        "deep_discovery_loops": 2,
        "deep_arena_loops": 0,
        "signoff_brief_model": "gpt-5.5-medium",
        "signoff_deep_model": "gpt-5.5-xhigh",
    }


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
                        "discovery_reviewer_count": 3,
                    }
                },
                "orchestrator": {
                    "stable_defaults": {
                        "signoff_brief_model": "gpt-5.4-high",
                        "discovery_loops": 2,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    lens = lens_model_config("review-plan", state_dir=state_dir)
    phase_gate = gate_config("phase_gate", state_dir=state_dir)
    pr_gate = gate_config("pr_gate", state_dir=state_dir)
    config = load_config(state_dir)

    assert lens.model == "gpt-5.4"
    assert lens.reasoning_effort == "high"
    assert phase_gate.discovery_variant_id == "gpt-5.4-medium"
    assert phase_gate.discovery_reviewer_count == 3
    assert phase_gate.signoff_variant_id == "gpt-5.4-high"
    assert phase_gate.discovery_loops == 2
    assert pr_gate.signoff_variant_id == "gpt-5.5-xhigh"
    assert (
        config["orchestrator"]["stable_defaults"]["discovery_brief_model"]
        == "gpt-5.4-medium"
    )
    assert (
        config["orchestrator"]["stable_defaults"]["signoff_brief_model"]
        == "gpt-5.4-high"
    )


def test_model_label_parser_accepts_gpt_5_6_max() -> None:
    assert parse_model_label("gpt-5.6-sol-max", field="model") == (
        "gpt-5.6-sol",
        "max",
        None,
    )
    assert parse_model_label("gpt-5.6-terra-max-fast", field="model") == (
        "gpt-5.6-terra",
        "max",
        "fast",
    )


def test_public_config_rejects_external_arena_publish(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "config.json").write_text(
        json.dumps({"privacy": {"arena_external_publish_enabled": True}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="arena_external_publish_enabled"):
        load_config(state_dir)


def test_public_config_rejects_invalid_orchestrator_selection(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "config.json").write_text(
        json.dumps({"orchestrator": {"selection": "scramble"}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="orchestrator.selection"):
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
