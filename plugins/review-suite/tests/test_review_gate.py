from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from review_gate import (
    GateSelection,
    _gate_partial_path,
    _gate_reviewer_count,
    _launch_gate_run,
    _print_live_gate_completed_run,
    _select_gate_variants,
    _snapshot_queue_item,
    aggregate_gate_records,
    cleanup_stale_gate_partials,
    run_gate_round,
    summarize_gate_round,
)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _gate_run(variant_id: str, *, verdict: str, elapsed: float = 12.0, tokens: int = 1000, cost: float = 0.01) -> dict[str, object]:
    return {
        "slot": "alpha",
        "variant_id": variant_id,
        "grade_blocked": verdict == "blocked",
        "grade_block_reason": "review_interrupted" if verdict == "blocked" else None,
        "elapsed_seconds": elapsed,
        "usage": {"input_tokens": tokens // 2, "output_tokens": tokens // 2},
        "cost_usd": cost,
    }


def test_select_gate_variants_prefers_least_used_distinct_champions(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _write_json(
        state_dir / "operational_state.json",
        {
            "generated_at": "2026-04-14T00:00:00Z",
            "task_classes": {
                "phase_review": {
                    "champion_variant_ids": ["a", "b", "c"],
                    "cooldowns": {},
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "champion",
                },
                "pr_review": {
                    "champion_variant_ids": [],
                    "cooldowns": {},
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "scramble",
                },
            },
        },
    )
    gate_runs = [
        {"task_class": "phase_gate", "runs": [{"variant_id": "a"}, {"variant_id": "a"}]},
        {"task_class": "phase_gate", "runs": [{"variant_id": "b"}, {"variant_id": "c"}]},
    ]
    (state_dir / "gate_runs.jsonl").write_text("\n".join(json.dumps(row) for row in gate_runs) + "\n", encoding="utf-8")
    roster = {
        "variants": [
            {"id": "a", "state": "active"},
            {"id": "b", "state": "active"},
            {"id": "c", "state": "active"},
        ]
    }

    selection = _select_gate_variants(roster=roster, state_dir=state_dir, gate_task_class="phase_gate")

    assert selection.mode == "multi_champion_4_pass"
    assert [variant["id"] for variant in selection.variants] == ["b", "c", "a", "b"]


def test_select_gate_variants_duplicates_single_champion(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _write_json(
        state_dir / "operational_state.json",
        {
            "generated_at": "2026-04-14T00:00:00Z",
            "task_classes": {
                "phase_review": {
                    "champion_variant_ids": ["solo"],
                    "cooldowns": {},
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "champion",
                },
                "pr_review": {
                    "champion_variant_ids": [],
                    "cooldowns": {},
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "scramble",
                },
            },
        },
    )
    roster = {"variants": [{"id": "solo", "state": "active"}]}

    selection = _select_gate_variants(roster=roster, state_dir=state_dir, gate_task_class="phase_gate")

    assert selection.mode == "4_pass"
    assert [variant["id"] for variant in selection.variants] == ["solo", "solo", "solo", "solo"]


def test_select_gate_variants_falls_back_to_legacy_singular_champion_field(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _write_json(
        state_dir / "operational_state.json",
        {
            "generated_at": "2026-04-14T00:00:00Z",
            "task_classes": {
                "phase_review": {
                    "champion_variant_id": "solo",
                    "champion_variant_ids": [],
                    "cooldowns": {},
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "champion",
                },
                "pr_review": {
                    "champion_variant_ids": [],
                    "cooldowns": {},
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "scramble",
                },
            },
        },
    )
    roster = {"variants": [{"id": "solo", "state": "active"}]}

    selection = _select_gate_variants(roster=roster, state_dir=state_dir, gate_task_class="phase_gate")

    assert selection.mode == "4_pass"
    assert [variant["id"] for variant in selection.variants] == ["solo", "solo", "solo", "solo"]


def test_select_gate_variants_prefers_non_cooling_champions(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _write_json(
        state_dir / "operational_state.json",
        {
            "generated_at": "2026-04-14T00:00:00Z",
            "task_classes": {
                "phase_review": {
                    "champion_variant_ids": ["a", "b", "c"],
                    "cooldowns": {"a": {"until": "2099-01-01T00:00:00Z", "failure_count": 1}},
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "champion",
                },
                "pr_review": {
                    "champion_variant_ids": [],
                    "cooldowns": {},
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "scramble",
                },
            },
        },
    )
    roster = {
        "variants": [
            {"id": "a", "state": "active"},
            {"id": "b", "state": "active"},
            {"id": "c", "state": "active"},
        ]
    }

    selection = _select_gate_variants(roster=roster, state_dir=state_dir, gate_task_class="phase_gate")

    assert selection.mode == "multi_champion_4_pass"
    assert [variant["id"] for variant in selection.variants] == ["b", "c", "b", "c"]


def test_select_gate_variants_uses_fallback_when_all_champions_are_cooling(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _write_json(
        state_dir / "operational_state.json",
        {
            "generated_at": "2026-04-14T00:00:00Z",
            "task_classes": {
                "pr_review": {
                    "champion_variant_ids": ["gpt-5.5-xhigh"],
                    "cooldowns": {"gpt-5.5-xhigh": {"until": "2099-01-01T00:00:00Z", "failure_count": 1}},
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "champion",
                },
                "phase_review": {
                    "champion_variant_ids": [],
                    "cooldowns": {},
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "scramble",
                },
            },
        },
    )
    roster = {
        "variants": [
            {"id": "gpt-5.5-xhigh", "state": "active", "task_classes": ["pr_review"]},
            {"id": "gpt-5.4-xhigh", "state": "active", "task_classes": ["pr_review"]},
        ]
    }

    selection = _select_gate_variants(roster=roster, state_dir=state_dir, gate_task_class="pr_gate")

    assert selection.mode == "cooldown_backup_double_pass"
    assert [variant["id"] for variant in selection.variants] == ["gpt-5.4-xhigh", "gpt-5.4-xhigh"]
    assert selection.champion_ids == ("gpt-5.5-xhigh",)


def test_select_gate_variants_uses_configured_primary_without_champions(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _write_json(
        state_dir / "operational_state.json",
        {
            "generated_at": "2026-04-14T00:00:00Z",
            "task_classes": {
                "phase_review": {
                    "champion_variant_ids": [],
                    "cooldowns": {"alpha": {"until": "2099-01-01T00:00:00Z", "failure_count": 1}},
                    "probation_variant_ids": ["probation"],
                    "stable_variant_ids": [],
                    "mode": "scramble",
                },
                "pr_review": {
                    "champion_variant_ids": [],
                    "cooldowns": {},
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "scramble",
                },
            },
        },
    )
    (state_dir / "runs.jsonl").write_text("", encoding="utf-8")
    roster = {
        "variants": [
            {"id": "gpt-5.5-medium", "state": "active", "model": "gpt-5.5", "reasoning_effort": "medium", "task_classes": ["phase_review"]},
            {"id": "gpt-5.4-medium", "state": "active", "model": "gpt-5.4", "reasoning_effort": "medium", "task_classes": ["phase_review"]},
            {"id": "alpha", "state": "active", "model": "gpt-5.4", "reasoning_effort": "xhigh", "task_classes": ["phase_review"]},
            {"id": "probation", "state": "active", "model": "gpt-5.4", "reasoning_effort": "xhigh", "task_classes": ["phase_review"]},
        ]
    }

    selection = _select_gate_variants(roster=roster, state_dir=state_dir, gate_task_class="phase_gate")

    assert selection.mode == "configured_primary_4_pass"
    assert [variant["id"] for variant in selection.variants] == [
        "gpt-5.4-medium",
        "gpt-5.4-medium",
        "gpt-5.4-medium",
        "gpt-5.4-medium",
    ]


def test_phase_gate_reviewer_count_uses_quad_only_for_first_scaffold_run(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = "feature/branch"

    assert (
        _gate_reviewer_count(
            gate_task_class="phase_gate",
            state_dir=state_dir,
            review_cwd=repo,
            task_id=task_id,
        )
        == 4
    )

    (state_dir / "gate_runs.jsonl").parent.mkdir(parents=True, exist_ok=True)
    (state_dir / "gate_runs.jsonl").write_text(
        json.dumps(
            {
                "task_class": "phase_gate",
                "task_id": task_id,
                "review_cwd_normalized": str(repo.resolve()),
                "runs": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert (
        _gate_reviewer_count(
            gate_task_class="phase_gate",
            state_dir=state_dir,
            review_cwd=repo,
            task_id=task_id,
        )
        == 4
    )

    (state_dir / "gate_runs.jsonl").write_text(
        json.dumps(
            {
                "task_class": "phase_gate",
                "task_id": task_id,
                "review_cwd_normalized": str(repo.resolve()),
                "signoff_status": "blocked",
                "runs": [{"review_status": "interrupted", "grade_blocked": True}],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert (
        _gate_reviewer_count(
            gate_task_class="phase_gate",
            state_dir=state_dir,
            review_cwd=repo,
            task_id=task_id,
        )
        == 4
    )

    (state_dir / "gate_runs.jsonl").write_text(
        json.dumps(
            {
                "task_class": "phase_gate",
                "task_id": task_id,
                "review_cwd_normalized": str(repo.resolve()),
                "signoff_status": "pending",
                "runs": [{"review_status": "completed", "grade_blocked": False}],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert (
        _gate_reviewer_count(
            gate_task_class="phase_gate",
            state_dir=state_dir,
            review_cwd=repo,
            task_id=task_id,
        )
        == 2
    )
    assert (
        _gate_reviewer_count(
            gate_task_class="phase_gate",
            state_dir=state_dir,
            review_cwd=repo,
            task_id="other",
        )
        == 4
    )
    assert _gate_reviewer_count(gate_task_class="pr_gate", state_dir=state_dir, review_cwd=repo, task_id=task_id) == 2


def test_select_gate_variants_champion_override_wins_over_configured_primary(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _write_json(
        state_dir / "operational_state.json",
        {
            "generated_at": "2026-04-24T00:00:00Z",
            "task_classes": {
                "phase_review": {
                    "champion_variant_ids": [],
                    "cooldowns": {},
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "scramble",
                },
                "pr_review": {
                    "champion_variant_ids": [],
                    "cooldowns": {},
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "scramble",
                },
            },
        },
    )
    roster = {
        "variants": [
            {"id": "gpt-5.5-medium", "state": "active", "task_classes": ["phase_review"]},
            {"id": "gpt-5.4-medium", "state": "active", "task_classes": ["phase_review"]},
        ]
    }

    selection = _select_gate_variants(
        roster=roster,
        state_dir=state_dir,
        gate_task_class="phase_gate",
        champion_override="gpt-5.4-medium",
    )

    assert selection.mode == "champion_override_4_pass"
    assert [variant["id"] for variant in selection.variants] == [
        "gpt-5.4-medium",
        "gpt-5.4-medium",
        "gpt-5.4-medium",
        "gpt-5.4-medium",
    ]
    assert selection.champion_ids == ("gpt-5.4-medium",)


def test_select_gate_variants_rejects_ineligible_champion_override(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _write_json(
        state_dir / "operational_state.json",
        {
            "generated_at": "2026-04-24T00:00:00Z",
            "task_classes": {
                "phase_review": {
                    "champion_variant_ids": [],
                    "cooldowns": {},
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "scramble",
                },
                "pr_review": {
                    "champion_variant_ids": [],
                    "cooldowns": {},
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "scramble",
                },
            },
        },
    )
    roster = {
        "variants": [
            {"id": "gpt-5.5-xhigh", "state": "active", "task_classes": ["pr_review"]},
        ]
    }

    with pytest.raises(ValueError, match="not eligible for phase_review"):
        _select_gate_variants(
            roster=roster,
            state_dir=state_dir,
            gate_task_class="phase_gate",
            champion_override="gpt-5.5-xhigh",
        )


def test_select_gate_variants_falls_through_provisional_backup_order_when_primary_is_cooling(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _write_json(
        state_dir / "operational_state.json",
        {
            "generated_at": "2026-04-15T00:00:00Z",
            "task_classes": {
                "phase_review": {
                    "champion_variant_ids": [],
                    "cooldowns": {
                        "gpt-5.4-medium": {"until": "2099-01-01T00:00:00Z", "failure_count": 1}
                    },
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "scramble",
                },
                "pr_review": {
                    "champion_variant_ids": [],
                    "cooldowns": {},
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "scramble",
                },
            },
        },
    )
    roster = {
        "variants": [
            {"id": "gpt-5.5-medium", "state": "active", "task_classes": ["phase_review"]},
            {"id": "gpt-5.4-medium", "state": "active", "task_classes": ["phase_review"]},
            {"id": "gpt-5.4-high", "state": "active", "task_classes": ["phase_review"]},
        ]
    }

    selection = _select_gate_variants(roster=roster, state_dir=state_dir, gate_task_class="phase_gate")

    assert selection.mode == "provisional_backup_4_pass"
    assert [variant["id"] for variant in selection.variants] == [
        "gpt-5.5-medium",
        "gpt-5.5-medium",
        "gpt-5.5-medium",
        "gpt-5.5-medium",
    ]


def test_select_gate_variants_excludes_probation_from_supplied_roster_fallback(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _write_json(
        state_dir / "operational_state.json",
        {
            "generated_at": "2026-04-15T00:00:00Z",
            "task_classes": {
                "phase_review": {
                    "champion_variant_ids": [],
                    "cooldowns": {},
                    "probation_variant_ids": ["probation"],
                    "stable_variant_ids": [],
                    "mode": "scramble",
                },
                "pr_review": {
                    "champion_variant_ids": [],
                    "cooldowns": {},
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "scramble",
                },
            },
        },
    )
    roster = {
        "variants": [
            {"id": "fallback", "state": "active", "task_classes": ["phase_review"]},
            {"id": "probation", "state": "active", "task_classes": ["phase_review"]},
        ]
    }

    selection = _select_gate_variants(roster=roster, state_dir=state_dir, gate_task_class="phase_gate")

    assert selection.mode == "provisional_backup_4_pass"
    assert [variant["id"] for variant in selection.variants] == ["fallback", "fallback", "fallback", "fallback"]


def test_select_gate_variants_uses_provisional_backup_for_pr_gate_without_champions(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _write_json(
        state_dir / "operational_state.json",
        {
            "generated_at": "2026-04-15T00:00:00Z",
            "task_classes": {
                "phase_review": {
                    "champion_variant_ids": [],
                    "cooldowns": {},
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "scramble",
                },
                "pr_review": {
                    "champion_variant_ids": [],
                    "cooldowns": {
                        "gpt-5.5-xhigh": {"until": "2099-01-01T00:00:00Z", "failure_count": 1}
                    },
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "scramble",
                },
            },
        },
    )
    roster = {
        "variants": [
            {"id": "gpt-5.5-xhigh", "state": "active", "task_classes": ["pr_review"]},
            {"id": "gpt-5.4-xhigh", "state": "active", "task_classes": ["pr_review"]},
            {"id": "gpt-5.4-high", "state": "active", "task_classes": ["pr_review"]},
        ]
    }

    selection = _select_gate_variants(roster=roster, state_dir=state_dir, gate_task_class="pr_gate")

    assert selection.mode == "provisional_backup_double_pass"
    assert [variant["id"] for variant in selection.variants] == ["gpt-5.4-xhigh", "gpt-5.4-xhigh"]


def test_snapshot_queue_item_preserves_retry_after() -> None:
    import review_gate

    original = review_gate.time.monotonic
    review_gate.time.monotonic = lambda: 10.0
    try:
        snapshot = _snapshot_queue_item(
            {
                "slot": "alpha",
                "variant": {"id": "alpha-model"},
                "retry_attempts": 1,
                "retry_after": 22.5,
            }
        )
    finally:
        review_gate.time.monotonic = original

    assert snapshot["retry_delay_seconds"] == 12.5


def test_aggregate_gate_records_tracks_sequences_and_model_metrics(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    operational_state = {
        "generated_at": "2026-04-14T00:00:00Z",
        "task_classes": {
            "phase_review": {
                "champion_variant_ids": ["alpha", "bravo"],
                "cooldowns": {},
                "probation_variant_ids": [],
                "stable_variant_ids": [],
                "mode": "champion",
            },
            "pr_review": {
                "champion_variant_ids": [],
                "cooldowns": {},
                "probation_variant_ids": [],
                "stable_variant_ids": [],
                "mode": "scramble",
            },
        },
    }
    records = [
        {
            "recorded_at": "2026-04-14T00:00:00Z",
            "task_class": "phase_gate",
            "task_id": "feature-a",
            "review_cwd_normalized": "repo",
            "green": False,
            "runs": [_gate_run("alpha", verdict="findings"), _gate_run("bravo", verdict="findings")],
        },
        {
            "recorded_at": "2026-04-14T00:10:00Z",
            "task_class": "phase_gate",
            "task_id": "feature-a",
            "review_cwd_normalized": "repo",
            "green": True,
            "runs": [_gate_run("alpha", verdict="clean"), _gate_run("bravo", verdict="clean")],
        },
        {
            "recorded_at": "2026-04-14T01:00:00Z",
            "task_class": "phase_gate",
            "task_id": "feature-b",
            "review_cwd_normalized": "repo",
            "green": True,
            "runs": [_gate_run("alpha", verdict="clean"), _gate_run("bravo", verdict="clean")],
        },
    ]
    _write_json(state_dir / "operational_state.json", operational_state)
    (state_dir / "gate_runs.jsonl").write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")

    summary = aggregate_gate_records(state_dir=state_dir, operational_state=operational_state)
    rows = {row["variant_id"]: row for row in summary["task_classes"]["phase_gate"]["leaderboard"]}

    assert rows["alpha"]["runs"] == 3
    assert rows["alpha"]["runs"] == 3
    assert rows["alpha"]["blocker_pct"] == 0.0


def test_aggregate_gate_records_skips_blocked_rounds_and_late_join_penalties(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    operational_state = {
        "generated_at": "2026-04-14T00:00:00Z",
        "task_classes": {
            "phase_review": {
                "champion_variant_ids": ["alpha", "bravo"],
                "cooldowns": {},
                "probation_variant_ids": [],
                "stable_variant_ids": [],
                "mode": "champion",
            },
            "pr_review": {
                "champion_variant_ids": [],
                "cooldowns": {},
                "probation_variant_ids": [],
                "stable_variant_ids": [],
                "mode": "scramble",
            },
        },
    }
    records = [
        {
            "recorded_at": "2026-04-14T00:00:00Z",
            "task_class": "phase_gate",
            "task_id": "feature-a",
            "review_cwd_normalized": "repo",
            "green": False,
            "runs": [_gate_run("alpha", verdict="blocked"), _gate_run("bravo", verdict="blocked")],
        },
        {
            "recorded_at": "2026-04-14T00:10:00Z",
            "task_class": "phase_gate",
            "task_id": "feature-a",
            "review_cwd_normalized": "repo",
            "green": False,
            "runs": [_gate_run("charlie", verdict="findings"), _gate_run("delta", verdict="findings")],
        },
        {
            "recorded_at": "2026-04-14T00:20:00Z",
            "task_class": "phase_gate",
            "task_id": "feature-a",
            "review_cwd_normalized": "repo",
            "green": True,
            "runs": [_gate_run("charlie", verdict="clean"), _gate_run("delta", verdict="clean")],
        },
    ]
    _write_json(state_dir / "operational_state.json", operational_state)
    (state_dir / "gate_runs.jsonl").write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")

    summary = aggregate_gate_records(state_dir=state_dir, operational_state=operational_state)
    rows = {row["variant_id"]: row for row in summary["task_classes"]["phase_gate"]["leaderboard"]}

    assert rows["charlie"]["runs"] == 2
    assert rows["charlie"]["blocker_pct"] == 0.0
    assert rows["alpha"]["runs"] == 1
    assert rows["alpha"]["blocker_pct"] == 100.0


def test_aggregate_gate_records_sorts_leaderboard_by_runs_before_blocker_rate(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    operational_state = {
        "generated_at": "2026-04-14T00:00:00Z",
        "task_classes": {
            "phase_review": {
                "champion_variant_ids": [],
                "cooldowns": {},
                "probation_variant_ids": [],
                "stable_variant_ids": [],
                "mode": "scramble",
            },
            "pr_review": {
                "champion_variant_ids": [],
                "cooldowns": {},
                "probation_variant_ids": [],
                "stable_variant_ids": [],
                "mode": "scramble",
            },
        },
    }
    records = [
        {
            "recorded_at": "2026-04-14T00:00:00Z",
            "task_class": "phase_gate",
            "task_id": "feature-a",
            "review_cwd_normalized": "repo",
            "runs": [_gate_run("many", verdict="blocked"), _gate_run("few", verdict="clean")],
        },
        {
            "recorded_at": "2026-04-14T00:10:00Z",
            "task_class": "phase_gate",
            "task_id": "feature-b",
            "review_cwd_normalized": "repo",
            "runs": [_gate_run("many", verdict="clean")],
        },
    ]
    _write_json(state_dir / "operational_state.json", operational_state)
    (state_dir / "gate_runs.jsonl").write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")

    summary = aggregate_gate_records(state_dir=state_dir, operational_state=operational_state)
    rows = summary["task_classes"]["phase_gate"]["leaderboard"]

    assert [row["variant_id"] for row in rows[:2]] == ["many", "few"]


def test_aggregate_gate_records_reports_gate_primary_for_gate_champions(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    operational_state = {
        "generated_at": "2026-04-14T00:00:00Z",
        "task_classes": {
            "phase_review": {
                "champion_variant_id": "solo",
                "champion_variant_ids": [],
                "cooldowns": {},
                "probation_variant_ids": [],
                "stable_variant_ids": [],
                "mode": "champion",
            },
            "pr_review": {
                "champion_variant_ids": [],
                "cooldowns": {},
                "probation_variant_ids": [],
                "stable_variant_ids": [],
                "mode": "scramble",
            },
        },
    }
    _write_json(state_dir / "operational_state.json", operational_state)
    (state_dir / "gate_runs.jsonl").write_text("", encoding="utf-8")

    summary = aggregate_gate_records(state_dir=state_dir, operational_state=operational_state)

    assert summary["task_classes"]["phase_gate"]["champions"] == ["gpt-5.4-medium"]
    assert summary["task_classes"]["phase_gate"]["leaderboard"] == [
        {
            "variant_id": "gpt-5.4-medium",
            "variant_label": "gpt-5.4-medium",
            "runs": 0,
            "blocker_pct": None,
            "median_elapsed_seconds": None,
            "median_total_tokens": None,
            "median_cost_usd": None,
        }
    ]


def test_summarize_gate_round_reports_signoff_pending_and_public_task_name() -> None:
    payload, exit_code = summarize_gate_round(
        gate_task_class="phase_gate",
        round_id="gate-1",
        task_id="branch-1",
        mode="dual_champion",
        champion_ids=("alpha", "bravo"),
        review_scope={"base": "main"},
        runs=[
            {
                "slot": "alpha",
                "variant_id": "alpha",
                "review_status": "completed",
                "status_summary": "No findings.",
                "grade_blocked": False,
                "grade_block_reason": None,
                "session_id": "session-a",
                "tokens_used": 123,
                "cost_usd": 0.01,
                "reviewer_output_ref": "ref://a",
                "reviewer_output": "No findings.",
            },
            {
                "slot": "bravo",
                "variant_id": "bravo",
                "review_status": "completed",
                "status_summary": "No findings.",
                "grade_blocked": False,
                "grade_block_reason": None,
                "session_id": "session-b",
                "tokens_used": 124,
                "cost_usd": 0.02,
                "reviewer_output_ref": "ref://b",
                "reviewer_output": "No findings.",
            },
        ],
    )

    assert exit_code == 0
    assert payload["round_id"] == "gate-1"
    assert payload["task"] == "review_t2"
    assert payload["status"] == "signoff_pending"
    assert payload["blocked"] is False
    assert payload["signoff_required"] is True
    assert "note" not in payload
    assert "policy" not in payload
    assert "scope_check" not in payload
    assert "mode" not in payload
    assert "target" not in payload
    assert "champions" not in payload
    assert "output" not in payload["runs"][0]
    assert "verdict" not in payload["runs"][0]
    assert "model" not in payload["runs"][0]
    assert "session" not in payload["runs"][0]
    assert "tokens" not in payload["runs"][0]
    assert "cost" not in payload["runs"][0]


def test_summarize_gate_round_omits_telemetry_from_default_payload() -> None:
    payload, exit_code = summarize_gate_round(
        gate_task_class="phase_gate",
        round_id="gate-usage",
        task_id="branch-usage",
        mode="double_pass",
        champion_ids=("alpha",),
        review_scope={"base": "main"},
        runs=[
            {
                "slot": "alpha",
                "variant_id": "alpha",
                "review_status": "completed",
                "status_summary": "No findings.",
                "grade_blocked": False,
                "grade_block_reason": None,
                "session_id": "session-a",
                "usage": {"input_tokens": 50, "output_tokens": 70},
                "tokens_used": None,
                "cost_usd": 0.01,
                "reviewer_output": "No findings.",
            },
            {
                "slot": "bravo",
                "variant_id": "alpha",
                "review_status": "completed",
                "status_summary": "No findings.",
                "grade_blocked": False,
                "grade_block_reason": None,
                "session_id": "session-b",
                "usage": {"input_tokens": 60, "output_tokens": 80},
                "tokens_used": None,
                "cost_usd": 0.02,
                "reviewer_output": "No findings.",
            },
        ],
    )

    assert exit_code == 0
    assert payload["status"] == "signoff_pending"
    assert "tokens" not in payload["runs"][0]
    assert "tokens" not in payload["runs"][1]
    assert "cost" not in payload["runs"][0]
    assert "session" not in payload["runs"][0]
    assert "output" not in payload["runs"][0]


def test_print_live_gate_completed_run_uses_status_not_review_content(capsys) -> None:
    _print_live_gate_completed_run(
        {
            "slot": "alpha",
            "review_status": "completed",
            "status_summary": "P2 - concise finding summary",
            "reviewer_output": "P2 - concise finding summary\n\nLong reviewer body with details.",
        }
    )

    captured = capsys.readouterr()
    assert "alpha: completed" in captured.err
    assert "P2 - concise finding summary" not in captured.err
    assert "Long reviewer body" not in captured.err


def test_summarize_gate_round_returns_nonzero_for_blocked_rounds() -> None:
    payload, exit_code = summarize_gate_round(
        gate_task_class="pr_gate",
        round_id="gate-2",
        task_id="branch-2",
        mode="dual_champion",
        champion_ids=("alpha", "bravo"),
        review_scope={"base": "main"},
        runs=[
            {
                "slot": "alpha",
                "variant_id": "alpha",
                "review_status": "timeout",
                "status_summary": "timed out",
                "grade_blocked": True,
                "grade_block_reason": "review_timed_out",
            },
            {
                "slot": "bravo",
                "variant_id": "bravo",
                "review_status": "completed",
                "status_summary": "No findings.",
                "grade_blocked": False,
                "grade_block_reason": None,
            },
        ],
    )

    assert exit_code == 1
    assert payload["status"] == "blocked"
    assert payload["blocked"] is True


def test_summarize_gate_round_keeps_signoff_pending_for_non_blocked_outputs() -> None:
    payload, exit_code = summarize_gate_round(
        gate_task_class="phase_gate",
        round_id="gate-3",
        task_id="branch-3",
        mode="dual_champion",
        champion_ids=("alpha", "bravo"),
        review_scope={"base": "main"},
        runs=[
            {
                "slot": "alpha",
                "variant_id": "alpha",
                "review_status": "completed",
                "status_summary": "finding",
                "grade_blocked": False,
                "grade_block_reason": None,
            },
            {
                "slot": "bravo",
                "variant_id": "bravo",
                "review_status": "completed",
                "status_summary": "No findings.",
                "grade_blocked": False,
                "grade_block_reason": None,
            },
        ],
    )

    assert exit_code == 0
    assert payload["status"] == "signoff_pending"


def test_run_gate_round_retries_operational_block_once(monkeypatch, tmp_path: Path, capsys) -> None:
    state_dir = tmp_path / "state"
    review_cwd = tmp_path / "repo"
    review_cwd.mkdir()
    _write_json(
        state_dir / "operational_state.json",
        {
            "generated_at": "2026-04-14T00:00:00Z",
            "task_classes": {
                "phase_review": {
                    "champion_variant_ids": ["alpha-model", "bravo-model"],
                    "cooldowns": {},
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "champion",
                },
                "pr_review": {
                    "champion_variant_ids": [],
                    "cooldowns": {},
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "scramble",
                },
            },
        },
    )

    class FakeProc:
        def poll(self) -> int:
            return 0

        def wait(self) -> int:
            return 0

        def kill(self) -> None:
            return None

    monkeypatch.setattr("review_gate.subprocess.Popen", FakeProc)
    monkeypatch.setattr("review_gate.ensure_clean_git_worktree", lambda *args, **kwargs: None)
    monkeypatch.setattr("review_gate.load_roster", lambda path: {})
    monkeypatch.setattr(
        "review_gate._select_gate_variants",
        lambda **kwargs: GateSelection(
            gate_task_class="phase_gate",
            arena_task_class="phase_review",
            mode="dual_champion",
            variants=(
                {"id": "alpha-model", "model": "gpt-5.4", "reasoning_effort": "xhigh"},
                {"id": "bravo-model", "model": "gpt-5.4", "reasoning_effort": "xhigh"},
            ),
            champion_ids=("alpha-model", "bravo-model"),
        ),
    )
    monkeypatch.setattr("review_gate.make_round_id", lambda *args, **kwargs: "gate-1")
    monkeypatch.setattr("review_gate._current_branch_name", lambda path: "feature/test")
    monkeypatch.setattr("review_gate.time.sleep", lambda seconds: None)

    launches: list[tuple[str, int]] = []

    def fake_launch_gate_run(*, slot: str, variant: dict[str, object], retry_attempts: int, **kwargs) -> dict[str, object]:
        launches.append((slot, retry_attempts))
        stdout_path = tmp_path / f"{slot}-{retry_attempts}.stdout"
        stderr_path = tmp_path / f"{slot}-{retry_attempts}.stderr"
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return {
            "slot": slot,
            "variant": dict(variant),
            "variant_id": str(variant["id"]),
            "process": FakeProc(),
            "stdout_path": stdout_path,
            "stderr_path": stderr_path,
            "started_at": "2026-04-14T00:00:00Z",
            "started_monotonic": 0.0,
            "title": f"{slot}-{retry_attempts}",
            "command": [],
            "retry_attempts": retry_attempts,
        }

    monkeypatch.setattr("review_gate._launch_gate_run", fake_launch_gate_run)

    attempts = {"alpha": 0, "bravo": 0}

    def fake_collect_completed_review_capture(*, slot: str, variant_id: str, **kwargs) -> dict[str, object]:
        attempts[slot] += 1
        if slot == "alpha" and attempts[slot] == 1:
            return {
                "slot": slot,
                "variant_id": variant_id,
                "review_status": "interrupted",
                "status_summary": "interrupted",
                "grade_blocked": True,
                "grade_block_reason": "review_interrupted",
                "reviewer_output": "",
                "reviewer_output_ref": None,
                "usage": {},
                "cost_usd": None,
                "tokens_used": None,
                "elapsed_seconds": 1.0,
            }
        return {
            "slot": slot,
            "variant_id": variant_id,
            "review_status": "completed",
            "status_summary": "No findings.",
            "grade_blocked": False,
            "grade_block_reason": None,
            "reviewer_output": "I did not find any actionable bugs in this diff.",
            "reviewer_output_ref": None,
            "usage": {},
            "cost_usd": None,
            "tokens_used": None,
            "elapsed_seconds": 1.0,
        }

    monkeypatch.setattr("review_gate.collect_completed_review_capture", fake_collect_completed_review_capture)
    monkeypatch.setattr("review_gate._print_live_gate_completed_run", lambda run: None)
    cost_refreshes: list[dict[str, object]] = []
    monkeypatch.setattr(
        "review_gate.refresh_review_cost_report_best_effort",
        lambda **kwargs: cost_refreshes.append(kwargs) or None,
    )

    payload, exit_code = run_gate_round(
        gate_task_class="phase_gate",
        review_cwd=review_cwd,
        roster_path=tmp_path / "roster.json",
        state_dir=state_dir,
        sqlite_path=tmp_path / "state.sqlite",
        task_id=None,
        allow_dirty=True,
        progress_interval_seconds=15,
        timeout_seconds=0,
        allow_unsafe_windows_wsl_fallback=False,
        review_scope={"base": "main"},
        prompt="",
    )

    assert launches == [("alpha", 0), ("alpha", 1), ("bravo", 0)]
    assert exit_code == 0
    assert payload["status"] == "signoff_pending"
    assert "close-gate" in payload["action"]["cmd"]
    assert "--verdict VERDICT" in payload["action"]["cmd"]
    assert payload["action"]["verdict"] == ["clean", "findings"]
    assert "show_cmd" not in payload["action"]
    assert "scope_check" not in payload["action"]
    stored = [json.loads(line) for line in (state_dir / "gate_runs.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(stored) == 1
    assert stored[0]["signoff_status"] == "pending"
    assert len(stored[0]["retry_runs"]) == 1
    assert stored[0]["retry_runs"][0]["grade_block_reason"] == "review_interrupted"
    assert cost_refreshes == [{"state_dir": state_dir, "review_cwd": review_cwd}]
    captured = capsys.readouterr()
    assert "Output:" in captured.out
    assert "I did not find any actionable bugs in this diff." in captured.out
    assert "reviews can take up to 20m" in captured.err
    assert "alpha-model" not in captured.err
    assert "bravo-model" not in captured.err
    assert "xhigh" not in captured.err


def test_run_gate_round_replaces_exhausted_gate_reviewer_with_inline_fallback(monkeypatch, tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    review_cwd = tmp_path / "repo"
    review_cwd.mkdir()
    _write_json(
        state_dir / "operational_state.json",
        {
            "generated_at": "2026-05-04T00:00:00Z",
            "task_classes": {
                "phase_review": {
                    "champion_variant_ids": ["gpt-5.4-medium", "bravo-model"],
                    "cooldowns": {},
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "champion",
                },
                "pr_review": {
                    "champion_variant_ids": [],
                    "cooldowns": {},
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "champion",
                },
            },
        },
    )

    class FakeProc:
        pid = 123

        def poll(self):
            return 0

        def wait(self):
            return 0

    roster = {
        "variants": [
            {"id": "gpt-5.4-medium", "model": "gpt-5.4", "reasoning_effort": "medium", "task_classes": ["phase_review"]},
            {"id": "gpt-5.5-medium", "model": "gpt-5.5", "reasoning_effort": "medium", "task_classes": ["phase_review"]},
            {"id": "bravo-model", "model": "gpt-5.5", "reasoning_effort": "medium", "task_classes": ["phase_review"]},
        ]
    }
    monkeypatch.setattr("review_gate.subprocess.Popen", FakeProc)
    monkeypatch.setattr("review_gate.ensure_clean_git_worktree", lambda *args, **kwargs: None)
    monkeypatch.setattr("review_gate.load_roster", lambda path: roster)
    monkeypatch.setattr("review_gate._gate_retry_delay_seconds", lambda reason: 0)
    monkeypatch.setattr("review_gate.OPERATIONAL_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr("review_gate.pending_launch_ready", lambda **kwargs: True)
    monkeypatch.setattr(
        "review_gate._select_gate_variants",
        lambda **kwargs: GateSelection(
            gate_task_class="phase_gate",
            arena_task_class="phase_review",
            mode="double_pass",
            variants=(roster["variants"][0], roster["variants"][2]),
            champion_ids=("gpt-5.4-medium", "bravo-model"),
        ),
    )
    monkeypatch.setattr("review_gate.make_round_id", lambda *args, **kwargs: "gate-inline-fallback")
    monkeypatch.setattr("review_gate._current_branch_name", lambda path: "feature/test")
    monkeypatch.setattr("review_gate.time.sleep", lambda seconds: None)

    launches: list[tuple[str, str, int]] = []

    def fake_launch_gate_run(*, slot: str, variant: dict[str, object], retry_attempts: int, **kwargs) -> dict[str, object]:
        variant_id = str(variant["id"])
        launches.append((slot, variant_id, retry_attempts))
        stdout_path = tmp_path / f"{slot}-{variant_id}-{retry_attempts}.stdout"
        stderr_path = tmp_path / f"{slot}-{variant_id}-{retry_attempts}.stderr"
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return {
            "slot": slot,
            "variant": dict(variant),
            "variant_id": variant_id,
            "process": FakeProc(),
            "stdout_path": stdout_path,
            "stderr_path": stderr_path,
            "started_at": "2026-05-04T00:00:00Z",
            "started_monotonic": 0.0,
            "title": f"{slot}-{variant_id}-{retry_attempts}",
            "command": [],
            "retry_attempts": retry_attempts,
        }

    monkeypatch.setattr("review_gate._launch_gate_run", fake_launch_gate_run)

    def fake_collect_completed_review_capture(*, slot: str, variant_id: str, **kwargs) -> dict[str, object]:
        blocked = slot == "alpha" and variant_id == "gpt-5.4-medium"
        return {
            "slot": slot,
            "variant_id": variant_id,
            "review_status": "interrupted" if blocked else "completed",
            "status_summary": "interrupted" if blocked else "No findings.",
            "grade_blocked": blocked,
            "grade_block_reason": "review_interrupted" if blocked else None,
            "reviewer_output": "" if blocked else "No findings.",
            "reviewer_output_ref": None,
            "usage": {},
            "cost_usd": None,
            "tokens_used": None,
            "elapsed_seconds": 1.0,
        }

    monkeypatch.setattr("review_gate.collect_completed_review_capture", fake_collect_completed_review_capture)
    monkeypatch.setattr("review_gate._print_live_gate_completed_run", lambda run: None)
    monkeypatch.setattr("review_gate.refresh_review_cost_report_best_effort", lambda **kwargs: None)

    payload, exit_code = run_gate_round(
        gate_task_class="phase_gate",
        review_cwd=review_cwd,
        roster_path=tmp_path / "roster.json",
        state_dir=state_dir,
        sqlite_path=tmp_path / "state.sqlite",
        task_id=None,
        allow_dirty=True,
        progress_interval_seconds=15,
        timeout_seconds=0,
        allow_unsafe_windows_wsl_fallback=False,
        review_scope={"base": "main"},
        prompt="",
    )

    assert exit_code == 0
    assert payload["status"] == "signoff_pending"
    assert ("alpha", "gpt-5.5-medium", 0) in launches
    stored = [json.loads(line) for line in (state_dir / "gate_runs.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [run["variant_id"] for run in stored[0]["runs"]] == ["bravo-model", "gpt-5.5-medium"]
    assert len(stored[0]["retry_runs"]) == 2
    assert stored[0]["retry_runs"][-1]["cooldown_eligible"] is True
    op_state = json.loads((state_dir / "operational_state.json").read_text(encoding="utf-8"))
    cooldown = op_state["task_classes"]["phase_review"]["cooldowns"]["gpt-5.4-medium"]
    assert cooldown["last_reason"] == "review_interrupted"


def test_run_gate_round_inline_fallback_skips_cooling_backup(monkeypatch, tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    review_cwd = tmp_path / "repo"
    review_cwd.mkdir()
    _write_json(
        state_dir / "operational_state.json",
        {
            "generated_at": "2026-05-04T00:00:00Z",
            "task_classes": {
                "phase_review": {
                    "champion_variant_ids": ["gpt-5.4-medium", "bravo-model"],
                    "cooldowns": {"gpt-5.5-medium": {"until": "2099-01-01T00:00:00Z", "failure_count": 1}},
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "champion",
                },
                "pr_review": {
                    "champion_variant_ids": [],
                    "cooldowns": {},
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "champion",
                },
            },
        },
    )

    class FakeProc:
        pid = 123

        def poll(self):
            return 0

        def wait(self):
            return 0

    roster = {
        "variants": [
            {"id": "gpt-5.4-medium", "model": "gpt-5.4", "reasoning_effort": "medium", "task_classes": ["phase_review"]},
            {"id": "gpt-5.5-medium", "model": "gpt-5.5", "reasoning_effort": "medium", "task_classes": ["phase_review"]},
            {"id": "gpt-5.4-high", "model": "gpt-5.4", "reasoning_effort": "high", "task_classes": ["phase_review"]},
            {"id": "bravo-model", "model": "gpt-5.5", "reasoning_effort": "medium", "task_classes": ["phase_review"]},
        ]
    }
    monkeypatch.setattr("review_gate.subprocess.Popen", FakeProc)
    monkeypatch.setattr("review_gate.ensure_clean_git_worktree", lambda *args, **kwargs: None)
    monkeypatch.setattr("review_gate.load_roster", lambda path: roster)
    monkeypatch.setattr("review_gate._gate_retry_delay_seconds", lambda reason: 0)
    monkeypatch.setattr("review_gate.OPERATIONAL_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr("review_gate.pending_launch_ready", lambda **kwargs: True)
    monkeypatch.setattr(
        "review_gate._select_gate_variants",
        lambda **kwargs: GateSelection(
            gate_task_class="phase_gate",
            arena_task_class="phase_review",
            mode="double_pass",
            variants=(roster["variants"][0], roster["variants"][3]),
            champion_ids=("gpt-5.4-medium", "bravo-model"),
        ),
    )
    monkeypatch.setattr("review_gate.make_round_id", lambda *args, **kwargs: "gate-inline-cooling-fallback")
    monkeypatch.setattr("review_gate._current_branch_name", lambda path: "feature/test")
    monkeypatch.setattr("review_gate.time.sleep", lambda seconds: None)

    launches: list[tuple[str, str, int]] = []

    def fake_launch_gate_run(*, slot: str, variant: dict[str, object], retry_attempts: int, **kwargs) -> dict[str, object]:
        variant_id = str(variant["id"])
        launches.append((slot, variant_id, retry_attempts))
        stdout_path = tmp_path / f"{slot}-{variant_id}-{retry_attempts}.stdout"
        stderr_path = tmp_path / f"{slot}-{variant_id}-{retry_attempts}.stderr"
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return {
            "slot": slot,
            "variant": dict(variant),
            "variant_id": variant_id,
            "process": FakeProc(),
            "stdout_path": stdout_path,
            "stderr_path": stderr_path,
            "started_at": "2026-05-04T00:00:00Z",
            "started_monotonic": 0.0,
            "title": f"{slot}-{variant_id}-{retry_attempts}",
            "command": [],
            "retry_attempts": retry_attempts,
        }

    monkeypatch.setattr("review_gate._launch_gate_run", fake_launch_gate_run)
    monkeypatch.setattr(
        "review_gate.collect_completed_review_capture",
        lambda slot, variant_id, **kwargs: {
            "slot": slot,
            "variant_id": variant_id,
            "review_status": "interrupted" if slot == "alpha" and variant_id == "gpt-5.4-medium" else "completed",
            "status_summary": "interrupted" if slot == "alpha" and variant_id == "gpt-5.4-medium" else "No findings.",
            "grade_blocked": slot == "alpha" and variant_id == "gpt-5.4-medium",
            "grade_block_reason": "review_interrupted" if slot == "alpha" and variant_id == "gpt-5.4-medium" else None,
            "reviewer_output": "",
            "reviewer_output_ref": None,
            "usage": {},
            "cost_usd": None,
            "tokens_used": None,
            "elapsed_seconds": 1.0,
        },
    )
    monkeypatch.setattr("review_gate._print_live_gate_completed_run", lambda run: None)
    monkeypatch.setattr("review_gate.refresh_review_cost_report_best_effort", lambda **kwargs: None)

    payload, exit_code = run_gate_round(
        gate_task_class="phase_gate",
        review_cwd=review_cwd,
        roster_path=tmp_path / "roster.json",
        state_dir=state_dir,
        sqlite_path=tmp_path / "state.sqlite",
        task_id=None,
        allow_dirty=True,
        progress_interval_seconds=15,
        timeout_seconds=0,
        allow_unsafe_windows_wsl_fallback=False,
        review_scope={"base": "main"},
        prompt="",
    )

    assert exit_code == 0
    assert payload["status"] == "signoff_pending"
    assert ("alpha", "gpt-5.5-medium", 0) not in launches
    assert ("alpha", "gpt-5.4-high", 0) in launches


def test_launch_gate_run_preserves_retry_attempts(monkeypatch, tmp_path: Path) -> None:
    class FakeProc:
        def __init__(self, *args, **kwargs) -> None:
            self.pid = 123
            self.stdin = None

    monkeypatch.setattr("review_gate.subprocess.Popen", FakeProc)
    monkeypatch.setattr("review_gate.build_review_command", lambda **kwargs: ["codex", "review"])

    run = _launch_gate_run(
        gate_task_class="phase_gate",
        round_id="gate-1",
        slot="alpha",
        variant={"id": "alpha-model", "model": "gpt-5.4", "reasoning_effort": "xhigh"},
        review_cwd=tmp_path,
        review_scope={"base": "main"},
        prompt="",
        allow_unsafe_windows_wsl_fallback=False,
        retry_attempts=1,
    )

    try:
        assert run["retry_attempts"] == 1
    finally:
        for key in ("stdout_path", "stderr_path"):
            Path(run[key]).unlink(missing_ok=True)


def test_run_gate_round_resumes_partial_snapshot(monkeypatch, tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    review_cwd = tmp_path / "repo"
    review_cwd.mkdir()
    _write_json(
        state_dir / "operational_state.json",
        {
            "generated_at": "2026-04-14T00:00:00Z",
            "task_classes": {
                "phase_review": {
                    "champion_variant_ids": ["alpha-model", "bravo-model"],
                    "cooldowns": {},
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "champion",
                },
                "pr_review": {
                    "champion_variant_ids": [],
                    "cooldowns": {},
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "scramble",
                },
            },
        },
    )
    partial_path = _gate_partial_path(
        state_dir=state_dir,
        gate_task_class="phase_gate",
        review_cwd=review_cwd,
        task_id="feature/test",
        review_scope={"base": "main"},
    )
    partial_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path.write_text(
        json.dumps(
            {
                "round_id": "gate-1",
                "gate_task_class": "phase_gate",
                "task_id": "feature/test",
                "selection_mode": "dual_champion",
                "selection_champion_variant_ids": ["alpha-model", "bravo-model"],
                "selection_variants": [
                    {"id": "alpha-model", "model": "gpt-5.4", "reasoning_effort": "xhigh"},
                    {"id": "bravo-model", "model": "gpt-5.4", "reasoning_effort": "xhigh"},
                ],
                "round_started_at": "",
                "completed_runs": [
                    {
                        "slot": "alpha",
                        "variant_id": "alpha-model",
                        "review_status": "completed",
                        "status_summary": "No findings.",
                        "grade_blocked": False,
                        "grade_block_reason": None,
                        "elapsed_seconds": 1.0,
                        "session_id": None,
                        "usage": {},
                        "cost_usd": None,
                        "reviewer_output": "No findings.",
                        "reviewer_output_ref": None,
                    }
                ],
                "retry_runs": [],
                "pending": [],
                "waiting_retry": [],
                "active": [
                    {
                        "slot": "bravo",
                        "variant": {"id": "bravo-model", "model": "gpt-5.4", "reasoning_effort": "xhigh"},
                        "retry_attempts": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("review_gate._load_gate_partial", lambda path: json.loads(partial_path.read_text(encoding="utf-8")))
    monkeypatch.setattr("review_gate.utc_now_iso", lambda: "2026-04-14T00:00:30Z")

    class FakeProc:
        def poll(self) -> int:
            return 0

        def wait(self) -> int:
            return 0

        def kill(self) -> None:
            return None

    launches: list[str] = []

    def fake_launch_gate_run(*, slot: str, variant: dict[str, object], retry_attempts: int, **kwargs) -> dict[str, object]:
        launches.append(slot)
        stdout_path = tmp_path / f"{slot}.stdout"
        stderr_path = tmp_path / f"{slot}.stderr"
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return {
            "slot": slot,
            "variant": dict(variant),
            "variant_id": str(variant["id"]),
            "process": FakeProc(),
            "stdout_path": stdout_path,
            "stderr_path": stderr_path,
            "started_at": "2026-04-14T00:00:00Z",
            "started_monotonic": 0.0,
            "title": f"{slot}-0",
            "command": [],
            "retry_attempts": retry_attempts,
        }

    monkeypatch.setattr("review_gate.subprocess.Popen", FakeProc)
    monkeypatch.setattr("review_gate.ensure_clean_git_worktree", lambda *args, **kwargs: None)
    monkeypatch.setattr("review_gate.load_roster", lambda path: {})
    monkeypatch.setattr("review_gate._launch_gate_run", fake_launch_gate_run)
    monkeypatch.setattr(
        "review_gate.collect_completed_review_capture",
        lambda **kwargs: {
            "slot": "bravo",
            "variant_id": "bravo-model",
            "review_status": "completed",
            "status_summary": "No findings.",
            "grade_blocked": False,
            "grade_block_reason": None,
            "reviewer_output": "No findings.",
            "reviewer_output_ref": None,
            "usage": {},
            "cost_usd": None,
            "tokens_used": None,
            "elapsed_seconds": 1.0,
        },
    )
    monkeypatch.setattr("review_gate._print_live_gate_completed_run", lambda run: None)

    payload, exit_code = run_gate_round(
        gate_task_class="phase_gate",
        review_cwd=review_cwd,
        roster_path=tmp_path / "roster.json",
        state_dir=state_dir,
        sqlite_path=tmp_path / "state.sqlite",
        task_id="feature/test",
        allow_dirty=True,
        progress_interval_seconds=15,
        timeout_seconds=0,
        allow_unsafe_windows_wsl_fallback=False,
        review_scope={"base": "main"},
        prompt="",
    )

    assert launches == ["bravo"]
    assert exit_code == 0
    assert payload["status"] == "signoff_pending"
    records = [
        json.loads(line)
        for line in (state_dir / "gate_runs.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert records[0]["round_started_at"] == "2026-04-14T00:00:30Z"
    assert not partial_path.exists()


def test_cleanup_stale_gate_partials_archives_non_live_partial(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("review_gate.utc_now", lambda: datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc))
    monkeypatch.setattr("review_gate._process_is_running", lambda pid: False)
    partial_path = tmp_path / "gate_partials" / "pr_gate-deadbeef.json"
    _write_json(
        partial_path,
        {
            "round_id": "old-gate-round",
            "gate_task_class": "pr_gate",
            "round_started_at": "2026-05-02T11:59:00Z",
            "active": [{"slot": "alpha", "pid": 12345}],
            "pending": [],
            "waiting_retry": [],
            "completed_runs": [],
        },
    )

    cleaned = cleanup_stale_gate_partials(tmp_path)

    assert cleaned[0]["round_id"] == "old-gate-round"
    assert cleaned[0]["reason"] == "auto_stale_gate_partial_24h"
    assert not partial_path.exists()
    archived = list((tmp_path / "gate_partials" / "dismissed").glob("pr_gate-deadbeef-stale-*.json"))
    assert len(archived) == 1
    payload = json.loads(archived[0].read_text(encoding="utf-8"))
    assert payload["status"] == "dismissed"
    assert payload["dismissed_reason"] == "auto_stale_gate_partial_24h"


def test_cleanup_stale_gate_partials_keeps_live_partial(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("review_gate.utc_now", lambda: datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc))
    monkeypatch.setattr("review_gate._process_is_running", lambda pid: True)
    partial_path = tmp_path / "gate_partials" / "pr_gate-live.json"
    _write_json(
        partial_path,
        {
            "round_id": "old-live-gate-round",
            "gate_task_class": "pr_gate",
            "round_started_at": "2026-05-02T11:59:00Z",
            "active": [{"slot": "alpha", "pid": 12345}],
            "pending": [],
            "waiting_retry": [],
            "completed_runs": [],
        },
    )

    assert cleanup_stale_gate_partials(tmp_path) == []
    assert partial_path.exists()


def test_cleanup_stale_gate_partials_archives_recorded_final_partial(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("review_gate.utc_now", lambda: datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc))
    monkeypatch.setattr("review_gate._gate_round_already_recorded", lambda state_dir, round_id: True)
    partial_path = tmp_path / "gate_partials" / "pr_gate-final.json"
    _write_json(
        partial_path,
        {
            "round_id": "old-final-gate-round",
            "gate_task_class": "pr_gate",
            "round_started_at": "2026-05-02T11:59:00Z",
            "final_record": {
                "round_id": "old-final-gate-round",
                "review_completed_at": "2026-05-02T12:00:00Z",
                "runs": [],
            },
        },
    )

    cleaned = cleanup_stale_gate_partials(tmp_path)

    assert cleaned[0]["round_id"] == "old-final-gate-round"
    assert not partial_path.exists()
    assert list((tmp_path / "gate_partials" / "dismissed").glob("pr_gate-final-stale-*.json"))


def test_cleanup_stale_gate_partials_keeps_unrecorded_final_partial(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("review_gate.utc_now", lambda: datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc))
    monkeypatch.setattr("review_gate._gate_round_already_recorded", lambda state_dir, round_id: False)
    partial_path = tmp_path / "gate_partials" / "pr_gate-final-unrecorded.json"
    _write_json(
        partial_path,
        {
            "round_id": "old-final-gate-round",
            "gate_task_class": "pr_gate",
            "round_started_at": "2026-05-02T11:59:00Z",
            "final_record": {
                "round_id": "old-final-gate-round",
                "review_completed_at": "2026-05-02T12:00:00Z",
                "runs": [],
            },
        },
    )

    assert cleanup_stale_gate_partials(tmp_path) == []
    assert partial_path.exists()


def test_run_gate_round_preserves_waiting_retry_delay_on_resume(monkeypatch, tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    review_cwd = tmp_path / "repo"
    review_cwd.mkdir()
    _write_json(
        state_dir / "operational_state.json",
        {
            "generated_at": "2026-04-14T00:00:00Z",
            "task_classes": {
                "phase_review": {
                    "champion_variant_ids": ["alpha-model", "bravo-model"],
                    "cooldowns": {},
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "champion",
                },
                "pr_review": {
                    "champion_variant_ids": [],
                    "cooldowns": {},
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "scramble",
                },
            },
        },
    )
    partial_path = _gate_partial_path(
        state_dir=state_dir,
        gate_task_class="phase_gate",
        review_cwd=review_cwd,
        task_id="feature/test",
        review_scope={"base": "main"},
    )
    partial_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path.write_text(
        json.dumps(
            {
                "round_id": "gate-1",
                "gate_task_class": "phase_gate",
                "task_id": "feature/test",
                "selection_mode": "dual_champion",
                "selection_champion_variant_ids": ["alpha-model", "bravo-model"],
                "selection_variants": [
                    {"id": "alpha-model", "model": "gpt-5.4", "reasoning_effort": "xhigh"},
                    {"id": "bravo-model", "model": "gpt-5.4", "reasoning_effort": "xhigh"},
                ],
                "completed_runs": [],
                "retry_runs": [],
                "pending": [],
                "waiting_retry": [
                    {
                        "slot": "alpha",
                        "variant": {"id": "alpha-model", "model": "gpt-5.4", "reasoning_effort": "xhigh"},
                        "retry_attempts": 1,
                        "retry_delay_seconds": 10.0,
                    }
                ],
                "active": [
                    {
                        "slot": "bravo",
                        "variant": {"id": "bravo-model", "model": "gpt-5.4", "reasoning_effort": "xhigh"},
                        "retry_attempts": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("review_gate._load_gate_partial", lambda path: json.loads(partial_path.read_text(encoding="utf-8")))

    class FakeProc:
        def poll(self) -> int:
            return 0

        def wait(self) -> int:
            return 0

        def kill(self) -> None:
            return None

    clock = iter([0.0] * 8 + [11.0] * 8)
    monkeypatch.setattr("review_gate.time.monotonic", lambda: next(clock, 11.0))
    monkeypatch.setattr("review_gate.time.sleep", lambda seconds: None)

    launches: list[tuple[str, float, int]] = []

    def fake_launch_gate_run(*, slot: str, variant: dict[str, object], retry_attempts: int, **kwargs) -> dict[str, object]:
        launches.append((slot, __import__("review_gate").time.monotonic(), retry_attempts))
        stdout_path = tmp_path / f"{slot}.stdout"
        stderr_path = tmp_path / f"{slot}.stderr"
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return {
            "slot": slot,
            "variant": dict(variant),
            "variant_id": str(variant["id"]),
            "process": FakeProc(),
            "stdout_path": stdout_path,
            "stderr_path": stderr_path,
            "started_at": "2026-04-14T00:00:00Z",
            "started_monotonic": 11.0 if slot == "alpha" else 0.0,
            "title": f"{slot}-{retry_attempts}",
            "command": [],
            "retry_attempts": retry_attempts,
        }

    outcomes = {
        "alpha": {
            "slot": "alpha",
            "variant_id": "alpha-model",
            "review_status": "completed",
            "status_summary": "No findings.",
            "grade_blocked": False,
            "grade_block_reason": None,
            "reviewer_output": "No findings.",
            "reviewer_output_ref": None,
            "usage": {},
            "cost_usd": None,
            "tokens_used": None,
            "elapsed_seconds": 1.0,
        },
        "bravo": {
            "slot": "bravo",
            "variant_id": "bravo-model",
            "review_status": "completed",
            "status_summary": "No findings.",
            "grade_blocked": False,
            "grade_block_reason": None,
            "reviewer_output": "No findings.",
            "reviewer_output_ref": None,
            "usage": {},
            "cost_usd": None,
            "tokens_used": None,
            "elapsed_seconds": 1.0,
        },
    }

    monkeypatch.setattr("review_gate.subprocess.Popen", FakeProc)
    monkeypatch.setattr("review_gate.ensure_clean_git_worktree", lambda *args, **kwargs: None)
    monkeypatch.setattr("review_gate.load_roster", lambda path: {})
    monkeypatch.setattr("review_gate._launch_gate_run", fake_launch_gate_run)
    monkeypatch.setattr("review_gate.collect_completed_review_capture", lambda *, slot, **kwargs: outcomes[slot])
    monkeypatch.setattr("review_gate._print_live_gate_completed_run", lambda run: None)

    payload, exit_code = run_gate_round(
        gate_task_class="phase_gate",
        review_cwd=review_cwd,
        roster_path=tmp_path / "roster.json",
        state_dir=state_dir,
        sqlite_path=tmp_path / "state.sqlite",
        task_id="feature/test",
        allow_dirty=True,
        progress_interval_seconds=15,
        timeout_seconds=0,
        allow_unsafe_windows_wsl_fallback=False,
        review_scope={"base": "main"},
        prompt="",
    )

    assert launches == [("bravo", 0.0, 0), ("alpha", 11.0, 1)]
    assert exit_code == 0
    assert payload["status"] == "signoff_pending"


def test_run_gate_round_replays_sealed_final_partial_once(monkeypatch, tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    review_cwd = tmp_path / "repo"
    review_cwd.mkdir()
    _write_json(
        state_dir / "operational_state.json",
        {
            "generated_at": "2026-04-14T00:00:00Z",
            "task_classes": {
                "phase_review": {
                    "champion_variant_ids": ["alpha-model", "bravo-model"],
                    "cooldowns": {},
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "champion",
                },
                "pr_review": {
                    "champion_variant_ids": [],
                    "cooldowns": {},
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "scramble",
                },
            },
        },
    )
    partial_path = _gate_partial_path(
        state_dir=state_dir,
        gate_task_class="phase_gate",
        review_cwd=review_cwd,
        task_id="feature/test",
        review_scope={"base": "main"},
    )
    partial_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path.write_text(
        json.dumps(
            {
                "round_id": "gate-1",
                "gate_task_class": "phase_gate",
                "task_id": "feature/test",
                "selection_mode": "dual_champion",
                "selection_champion_variant_ids": ["alpha-model", "bravo-model"],
                "selection_variants": [
                    {"id": "alpha-model", "model": "gpt-5.4", "reasoning_effort": "xhigh"},
                    {"id": "bravo-model", "model": "gpt-5.4", "reasoning_effort": "xhigh"},
                ],
                "completed_runs": [],
                "retry_runs": [],
                "pending": [],
                "waiting_retry": [],
                "active": [],
                "final_record": {
                    "recorded_at": "2026-04-14T00:00:00Z",
                    "round_id": "gate-1",
                    "task_class": "phase_gate",
                    "arena_task_class": "phase_review",
                    "task_id": "feature/test",
                    "selection_mode": "dual_champion",
                    "selection_champion_variant_ids": ["alpha-model", "bravo-model"],
                    "review_cwd": str(review_cwd),
                    "review_cwd_normalized": str(review_cwd),
                    "review_scope": {"base": "main"},
                    "green": True,
                    "retry_runs": [],
                    "runs": [
                        {
                            "slot": "alpha",
                            "variant_id": "alpha-model",
                            "review_status": "completed",
                            "status_summary": "No findings.",
                            "grade_blocked": False,
                            "grade_block_reason": None,
                            "gate_verdict": "clean",
                            "gate_verdict_source": "deterministic",
                            "elapsed_seconds": 1.0,
                            "session_id": None,
                            "usage": {},
                            "cost_usd": None,
                            "reviewer_output": "No findings.",
                            "reviewer_output_ref": None,
                        },
                        {
                            "slot": "bravo",
                            "variant_id": "bravo-model",
                            "review_status": "completed",
                            "status_summary": "No findings.",
                            "grade_blocked": False,
                            "grade_block_reason": None,
                            "gate_verdict": "clean",
                            "gate_verdict_source": "deterministic",
                            "elapsed_seconds": 1.0,
                            "session_id": None,
                            "usage": {},
                            "cost_usd": None,
                            "reviewer_output": "No findings.",
                            "reviewer_output_ref": None,
                        },
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("review_gate.ensure_clean_git_worktree", lambda *args, **kwargs: None)
    monkeypatch.setattr("review_gate.load_roster", lambda path: {})

    payload, exit_code = run_gate_round(
        gate_task_class="phase_gate",
        review_cwd=review_cwd,
        roster_path=tmp_path / "roster.json",
        state_dir=state_dir,
        sqlite_path=tmp_path / "state.sqlite",
        task_id="feature/test",
        allow_dirty=True,
        progress_interval_seconds=15,
        timeout_seconds=0,
        allow_unsafe_windows_wsl_fallback=False,
        review_scope={"base": "main"},
        prompt="",
    )

    stored = [json.loads(line) for line in (state_dir / "gate_runs.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(stored) == 1
    assert stored[0]["round_id"] == "gate-1"
    assert exit_code == 0
    assert payload["status"] == "signoff_pending"
    assert not partial_path.exists()
    assert stored[0]["signoff_status"] == "pending"


def test_run_gate_round_replays_sealed_final_partial_refreshes_reports_when_already_recorded(monkeypatch, tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    review_cwd = tmp_path / "repo"
    review_cwd.mkdir()
    _write_json(
        state_dir / "operational_state.json",
        {
            "generated_at": "2026-04-14T00:00:00Z",
            "task_classes": {
                "phase_review": {
                    "champion_variant_ids": ["alpha-model", "bravo-model"],
                    "cooldowns": {},
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "champion",
                },
                "pr_review": {
                    "champion_variant_ids": [],
                    "cooldowns": {},
                    "probation_variant_ids": [],
                    "stable_variant_ids": [],
                    "mode": "scramble",
                },
            },
        },
    )
    partial_path = _gate_partial_path(
        state_dir=state_dir,
        gate_task_class="phase_gate",
        review_cwd=review_cwd,
        task_id="feature/test",
        review_scope={"base": "main"},
    )
    partial_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path.write_text(
        json.dumps(
            {
                "round_id": "gate-1",
                "gate_task_class": "phase_gate",
                "task_id": "feature/test",
                "selection_mode": "dual_champion",
                "selection_champion_variant_ids": ["alpha-model", "bravo-model"],
                "selection_variants": [
                    {"id": "alpha-model", "model": "gpt-5.4", "reasoning_effort": "xhigh"},
                    {"id": "bravo-model", "model": "gpt-5.4", "reasoning_effort": "xhigh"},
                ],
                "completed_runs": [],
                "retry_runs": [],
                "pending": [],
                "waiting_retry": [],
                "active": [],
                "final_record": {
                    "recorded_at": "2026-04-14T00:00:00Z",
                    "round_id": "gate-1",
                    "task_class": "phase_gate",
                    "arena_task_class": "phase_review",
                    "task_id": "feature/test",
                    "selection_mode": "dual_champion",
                    "selection_champion_variant_ids": ["alpha-model", "bravo-model"],
                    "review_cwd": str(review_cwd),
                    "review_cwd_normalized": str(review_cwd),
                    "review_scope": {"base": "main"},
                    "green": True,
                    "retry_runs": [],
                    "runs": [
                        {
                            "slot": "alpha",
                            "variant_id": "alpha-model",
                            "review_status": "completed",
                            "status_summary": "No findings.",
                            "grade_blocked": False,
                            "grade_block_reason": None,
                        "elapsed_seconds": 1.0,
                            "session_id": None,
                            "usage": {},
                            "cost_usd": None,
                            "reviewer_output": "No findings.",
                            "reviewer_output_ref": None,
                        },
                        {
                            "slot": "bravo",
                            "variant_id": "bravo-model",
                            "review_status": "completed",
                            "status_summary": "No findings.",
                            "grade_blocked": False,
                            "grade_block_reason": None,
                        "elapsed_seconds": 1.0,
                            "session_id": None,
                            "usage": {},
                            "cost_usd": None,
                            "reviewer_output": "No findings.",
                            "reviewer_output_ref": None,
                        },
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("review_gate.ensure_clean_git_worktree", lambda *args, **kwargs: None)
    monkeypatch.setattr("review_gate.load_roster", lambda path: {})
    monkeypatch.setattr("review_gate._gate_round_already_recorded", lambda *args, **kwargs: True)
    refreshed: list[str] = []
    monkeypatch.setattr("review_gate.refresh_gate_reports", lambda **kwargs: refreshed.append("yes") or {})

    payload, exit_code = run_gate_round(
        gate_task_class="phase_gate",
        review_cwd=review_cwd,
        roster_path=tmp_path / "roster.json",
        state_dir=state_dir,
        sqlite_path=tmp_path / "state.sqlite",
        task_id="feature/test",
        allow_dirty=True,
        progress_interval_seconds=15,
        timeout_seconds=0,
        allow_unsafe_windows_wsl_fallback=False,
        review_scope={"base": "main"},
        prompt="",
    )

    assert exit_code == 0
    assert payload["status"] == "signoff_pending"
    assert refreshed == ["yes"]
