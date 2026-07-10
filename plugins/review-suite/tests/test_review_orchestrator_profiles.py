from __future__ import annotations

import sys
from collections import Counter
from copy import deepcopy
from itertools import combinations
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from review_suite_core.config import load_config
from review_suite_core.orchestrator_profiles import (
    SUPPORTED_SELECTION_REASONS,
    OrchestratorProfileStep,
    load_orchestrator_profiles,
    resolve_orchestrator_profile,
)


def _step_summary(
    step: OrchestratorProfileStep,
) -> tuple[str, str, int | None, str | None, bool]:
    return (
        step.kind,
        step.name,
        step.count,
        step.reasoning_effort,
        step.rerun_on_findings,
    )


def test_default_stable_profiles_cover_all_modes(tmp_path: Path) -> None:
    config = load_config(tmp_path / "state")

    profiles = load_orchestrator_profiles(config)

    assert set(profiles["stable"]) == {"brief", "normal", "deep", "fast"}
    assert config["arena"]["enabled"] is False
    assert config["orchestrator"]["calibration"]["auto_promotion_enabled"] is False
    assert profiles["stable"]["brief"].deslop_enabled is True
    assert [_step_summary(step) for step in profiles["stable"]["brief"].steps] == [
        ("arena", "phase-discovery-brawl", None, None, False),
        ("review", "precision-signoff", 2, "medium", True),
    ]
    assert [step.name for step in profiles["stable"]["normal"].steps] == [
        "phase-discovery-brawl-1",
        "phase-discovery-brawl-2",
        "phase-discovery-brawl-3",
        "precision-signoff",
    ]
    assert [step.count for step in profiles["stable"]["normal"].steps] == [
        None,
        None,
        None,
        2,
    ]
    assert [step.name for step in profiles["stable"]["deep"].steps] == [
        "phase-discovery-brawl-1",
        "phase-discovery-brawl-2",
        "phase-discovery-brawl-3",
        "precision-signoff",
        "deep-discovery-brawl-1",
        "deep-discovery-brawl-2",
        "deep-signoff",
    ]
    assert [step.reasoning_effort for step in profiles["stable"]["deep"].steps] == [
        None,
        None,
        None,
        "medium",
        None,
        None,
        "xhigh",
    ]
    assert profiles["stable"]["normal"].steps[-1].rerun_on_findings is True
    assert profiles["stable"]["deep"].steps[3].rerun_on_findings is True
    assert profiles["stable"]["deep"].steps[-1].rerun_on_findings is True
    assert profiles["stable"]["fast"].deslop_enabled is False
    assert [_step_summary(step) for step in profiles["stable"]["fast"].steps] == [
        ("review", "fast-signoff", 2, "medium", False)
    ]
    assert profiles["stable"]["fast"].steps[0].max_review_rounds == 2
    assert set(profiles) == {"stable"}


def test_profile_step_kind_defaults_to_review(tmp_path: Path) -> None:
    config = deepcopy(load_config(tmp_path / "state"))
    config["orchestrator"]["profiles"]["stable"]["brief"]["steps"] = [
        {
            "name": "precision",
            "count": 1,
            "model": "gpt-5.5",
            "reasoning_effort": "medium",
        }
    ]

    profiles = load_orchestrator_profiles(config)

    assert profiles["stable"]["brief"].steps[0].kind == "review"


def test_profile_step_rejects_conflicting_findings_policies(tmp_path: Path) -> None:
    config = deepcopy(load_config(tmp_path / "state"))
    config["orchestrator"]["profiles"]["stable"]["brief"]["steps"] = [
        {
            "name": "precision",
            "count": 1,
            "model": "gpt-5.5",
            "reasoning_effort": "medium",
            "rerun_on_findings": True,
            "max_review_rounds": 2,
        }
    ]

    with pytest.raises(
        ValueError, match="cannot combine rerun_on_findings with max_review_rounds"
    ):
        load_orchestrator_profiles(config)


def test_stable_signoff_model_defaults_drive_profile_steps(tmp_path: Path) -> None:
    config = deepcopy(load_config(tmp_path / "state"))
    config["orchestrator"]["stable_defaults"].update(
        {
            "discovery_brief_model": "gpt-5.5-medium",
            "discovery_deep_model": "gpt-5.5-xhigh",
            "signoff_brief_model": "gpt-5.4-high",
            "signoff_deep_model": "gpt-5.4-xhigh",
        }
    )

    profiles = load_orchestrator_profiles(config)

    normal = profiles["stable"]["normal"].steps
    deep = profiles["stable"]["deep"].steps
    assert [(step.model, step.reasoning_effort) for step in normal if step.model] == [
        ("gpt-5.4", "high")
    ]
    assert [(step.model, step.reasoning_effort) for step in deep if step.model] == [
        ("gpt-5.4", "high"),
        ("gpt-5.4", "xhigh"),
    ]


def test_stable_discovery_loop_budgets_repeat_discovery_blocks(tmp_path: Path) -> None:
    config = deepcopy(load_config(tmp_path / "state"))
    config["orchestrator"]["stable_defaults"]["normal_discovery_loops"] = 2
    config["orchestrator"]["stable_defaults"]["deep_discovery_loops"] = 3

    profiles = load_orchestrator_profiles(config)

    assert [step.name for step in profiles["stable"]["deep"].steps] == [
        "phase-discovery-brawl-1",
        "phase-discovery-brawl-2",
        "precision-signoff",
        "deep-discovery-brawl-1",
        "deep-discovery-brawl-2",
        "deep-discovery-brawl-3",
        "deep-signoff",
    ]
    assert [step.rerun_on_findings for step in profiles["stable"]["deep"].steps] == [
        False,
        False,
        True,
        False,
        False,
        False,
        True,
    ]


def test_arena_disabled_omits_cohort_steps_but_keeps_discovery_brawls(
    tmp_path: Path,
) -> None:
    config = deepcopy(load_config(tmp_path / "state"))
    config["orchestrator"]["stable_defaults"]["normal_arena_loops"] = 2
    config["orchestrator"]["stable_defaults"]["deep_arena_loops"] = 1

    profiles = load_orchestrator_profiles(config)

    assert [step.kind for step in profiles["stable"]["normal"].steps] == [
        "arena",
        "arena",
        "arena",
        "review",
    ]
    assert [step.name for step in profiles["stable"]["normal"].steps] == [
        "phase-discovery-brawl-1",
        "phase-discovery-brawl-2",
        "phase-discovery-brawl-3",
        "precision-signoff",
    ]
    assert all(
        "arena-phase" not in step.name for step in profiles["stable"]["deep"].steps
    )
    assert all("arena-pr" not in step.name for step in profiles["stable"]["deep"].steps)


def test_disabled_first_step_in_loop_block_does_not_drop_enabled_steps(
    tmp_path: Path,
) -> None:
    config = deepcopy(load_config(tmp_path / "state"))
    config["arena"]["enabled"] = False
    config["orchestrator"]["stable_defaults"]["normal_discovery_loops"] = 2
    config["orchestrator"]["profiles"]["stable"]["normal"]["steps"] = [
        {
            "kind": "arena",
            "name": "arena-phase-review",
            "lane": "review_t1",
            "task_class": "phase_review",
            "loop_ref": "normal_discovery_loops",
            "enabled_ref": "arena.enabled",
        },
        {
            "name": "broad-discovery",
            "count": 1,
            "model_ref": "discovery_brief_model",
            "loop_ref": "normal_discovery_loops",
        },
    ]

    profiles = load_orchestrator_profiles(config)

    assert [step.name for step in profiles["stable"]["normal"].steps] == [
        "broad-discovery-1",
        "broad-discovery-2",
    ]


def test_arena_enabled_inserts_arena_steps_and_keeps_minimum_discovery(
    tmp_path: Path,
) -> None:
    config = deepcopy(load_config(tmp_path / "state"))
    config["arena"]["enabled"] = True
    config["orchestrator"]["stable_defaults"].update(
        {
            "normal_arena_loops": 2,
            "deep_arena_loops": 3,
        }
    )

    profiles = load_orchestrator_profiles(config)

    normal = profiles["stable"]["normal"].steps
    assert [(step.kind, step.name, step.lane, step.task_class) for step in normal] == [
        ("arena", "phase-discovery-brawl", "review_t1", "phase_review"),
        ("arena", "arena-phase-review-1", "review_t1", "phase_review"),
        ("arena", "arena-phase-review-2", "review_t1", "phase_review"),
        ("review", "precision-signoff", None, None),
    ]
    assert [step.reporting_pool for step in normal if step.kind == "arena"] == [
        False,
        True,
        True,
    ]

    deep = profiles["stable"]["deep"].steps
    assert [(step.kind, step.name, step.lane, step.task_class) for step in deep] == [
        ("arena", "phase-discovery-brawl", "review_t1", "phase_review"),
        ("arena", "arena-phase-review-1", "review_t1", "phase_review"),
        ("arena", "arena-phase-review-2", "review_t1", "phase_review"),
        ("review", "precision-signoff", None, None),
        ("arena", "deep-discovery-brawl", "review_t3", "pr_review"),
        ("arena", "arena-pr-review-1", "review_t3", "pr_review"),
        ("arena", "arena-pr-review-2", "review_t3", "pr_review"),
        ("arena", "arena-pr-review-3", "review_t3", "pr_review"),
        ("review", "deep-signoff", None, None),
    ]


def test_default_arena_pools_are_exact_fresh_balanced_cohorts(tmp_path: Path) -> None:
    config = load_config(tmp_path / "state")
    pools = config["arena"]["pools"]
    assert {pool["rating_pool_id"] for pool in pools.values()} == {
        "discovery-phase-gpt-5.6-v1",
        "discovery-deep-gpt-5.6-v1",
        "arena-phase-gpt-5.6-v1",
        "arena-deep-gpt-5.6-v1",
    }
    assert {name for name, pool in pools.items() if pool.get("reporting")} == {
        "arena_phase",
        "arena_deep",
    }
    assert pools["discovery_phase"]["variant_groups"] == [
        [
            "gpt-5.4-medium",
            "gpt-5.5-medium",
            "gpt-5.6-sol-medium",
            "gpt-5.6-terra-medium",
        ]
    ]
    assert pools["discovery_deep"]["variant_groups"] == [
        [
            "gpt-5.4-xhigh",
            "gpt-5.5-xhigh",
            "gpt-5.6-sol-xhigh",
            "gpt-5.6-terra-xhigh",
        ]
    ]
    expected = {
        "arena_phase": {
            "gpt-5.4-low",
            "gpt-5.4-medium",
            "gpt-5.5-low",
            "gpt-5.5-medium",
            "gpt-5.6-luna-medium",
            "gpt-5.6-luna-high",
            "gpt-5.6-luna-xhigh",
            "gpt-5.6-luna-max",
            "gpt-5.6-terra-medium",
            "gpt-5.6-terra-high",
            "gpt-5.6-terra-xhigh",
            "gpt-5.6-sol-low",
            "gpt-5.6-sol-medium",
        },
        "arena_deep": {
            "gpt-5.4-medium",
            "gpt-5.4-high",
            "gpt-5.4-xhigh",
            "gpt-5.5-medium",
            "gpt-5.5-high",
            "gpt-5.5-xhigh",
            "gpt-5.6-terra-medium",
            "gpt-5.6-terra-high",
            "gpt-5.6-terra-xhigh",
            "gpt-5.6-terra-max",
            "gpt-5.6-sol-medium",
            "gpt-5.6-sol-high",
            "gpt-5.6-sol-xhigh",
        },
    }
    for pool_name, candidates in expected.items():
        groups = pools[pool_name]["variant_groups"]
        assert len(groups) == 13
        assert {variant for group in groups for variant in group} == candidates
        assert Counter(variant for group in groups for variant in group) == Counter(
            {variant: 4 for variant in candidates}
        )
        pairs = Counter(
            pair for group in groups for pair in combinations(sorted(group), 2)
        )
        assert len(pairs) == 78
        assert set(pairs.values()) == {1}
        assert sum(map(len, groups)) == 52


def test_arena_steps_reject_mismatched_lane_and_task_class(tmp_path: Path) -> None:
    config = deepcopy(load_config(tmp_path / "state"))
    config["arena"]["enabled"] = True
    config["orchestrator"]["stable_defaults"]["normal_arena_loops"] = 1
    config["orchestrator"]["profiles"]["stable"]["normal"]["steps"][0]["lane"] = (
        "review_t1"
    )
    config["orchestrator"]["profiles"]["stable"]["normal"]["steps"][0]["task_class"] = (
        "pr_review"
    )

    with pytest.raises(
        ValueError, match="lane must be review_t3 for task_class pr_review"
    ):
        load_orchestrator_profiles(config)


def test_stable_model_refs_require_model_effort_labels(tmp_path: Path) -> None:
    config = deepcopy(load_config(tmp_path / "state"))
    config["orchestrator"]["stable_defaults"]["signoff_brief_model"] = "gpt-5.5"

    with pytest.raises(
        ValueError, match="orchestrator.stable_defaults.signoff_brief_model"
    ):
        load_orchestrator_profiles(config)


@pytest.mark.parametrize("mode", ["brief", "normal", "deep", "fast"])
def test_auto_selection_uses_stable_profile_when_configured(
    tmp_path: Path, mode: str
) -> None:
    config = load_config(tmp_path / "state")

    resolution = resolve_orchestrator_profile(config, mode=mode, selection="auto")

    assert resolution.requested_mode == mode
    assert resolution.effective_mode == mode
    assert resolution.requested_selection == "auto"
    assert resolution.effective_selection == "stable"
    assert resolution.selection_reason == "auto_stable_profile"
    assert resolution.steps


def test_explicit_stable_selection_records_reason_and_skips_grading(
    tmp_path: Path,
) -> None:
    config = load_config(tmp_path / "state")

    resolution = resolve_orchestrator_profile(config, mode="normal", selection="stable")

    assert resolution.effective_selection == "stable"
    assert resolution.selection_reason == "explicit_stable"
    assert resolution.selection_reason in SUPPORTED_SELECTION_REASONS


def test_benchmark_selection_is_not_supported(tmp_path: Path) -> None:
    config = load_config(tmp_path / "state")

    with pytest.raises(ValueError, match="orchestrator selection must be one of"):
        resolve_orchestrator_profile(config, mode="normal", selection="benchmark")


def test_auto_selection_requires_stable_profile(tmp_path: Path) -> None:
    config = deepcopy(load_config(tmp_path / "state"))
    del config["orchestrator"]["profiles"]["stable"]["normal"]

    with pytest.raises(
        ValueError, match="missing orchestrator stable profile for mode normal"
    ):
        resolve_orchestrator_profile(config, mode="normal", selection="auto")


def test_calibration_auto_promotion_is_not_enabled(tmp_path: Path) -> None:
    config = deepcopy(load_config(tmp_path / "state"))
    config["orchestrator"]["calibration"]["auto_promotion_enabled"] = True

    with pytest.raises(ValueError, match="auto_promotion_enabled must be false"):
        load_orchestrator_profiles(config)


def test_explicit_stable_selection_requires_stable_profile(tmp_path: Path) -> None:
    config = deepcopy(load_config(tmp_path / "state"))
    del config["orchestrator"]["profiles"]["stable"]["normal"]

    with pytest.raises(
        ValueError, match="missing orchestrator stable profile for mode normal"
    ):
        resolve_orchestrator_profile(config, mode="normal", selection="stable")
