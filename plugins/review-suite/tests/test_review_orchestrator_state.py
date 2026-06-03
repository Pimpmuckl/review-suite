from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from review_suite_core.orchestrator_state import (
    STAGE_CREATED,
    STAGE_FIX_PENDING,
    STAGE_GATE_RERUN_NEEDED,
    STAGE_LOCAL_GREEN_HANDOFF,
    STAGE_REVIEW_GREEN,
    STAGE_RETRY_REQUESTED,
    abort_cycle,
    can_advance_or_anchor,
    create_cycle,
    dismiss_recovery,
    mark_blocked,
    mark_crashed,
    mark_decision_pending,
    mark_deslop_closed,
    mark_deslop_failed,
    mark_fix_detected,
    mark_followup_review_pending,
    mark_gate_step_pending,
    mark_local_green_handoff,
    mark_retry_requested,
    mark_running,
    mark_review_step_pending,
    no_work_stage_is_idle,
    record_clean_decision,
    record_findings_decision,
    record_followup_clean,
    record_followup_findings,
    record_github_result,
)


def _cycle(tmp_path: Path, *, mode: str = "normal") -> dict[str, object]:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return create_cycle(
        cwd=repo,
        base="main",
        branch="feature/orchestrator",
        head="head-1",
        merge_base="base-1",
        requested_mode=mode,
        effective_mode=mode,
        selection="auto",
        effective_selection="stable",
    )


def test_create_cycle_is_compact_json_state_keyed_by_normalized_inputs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    state = create_cycle(
        cwd=repo,
        base="main",
        branch="feature/orchestrator",
        head="head-1",
        merge_base="base-1",
        requested_mode="normal",
        effective_mode="normal",
        selection="auto",
        effective_selection="stable",
    )
    same_state = create_cycle(
        cwd=repo / ".",
        base="main",
        branch="feature/orchestrator",
        head="head-1",
        merge_base="base-1",
        requested_mode="normal",
        effective_mode="normal",
        selection="auto",
        effective_selection="stable",
    )
    emergency = create_cycle(
        cwd=repo,
        base="main",
        branch="HEAD",
        head="head-1",
        merge_base="base-1",
        requested_mode="emergency",
        effective_mode="emergency",
        selection="stable",
    )

    assert json.loads(json.dumps(state)) == state
    assert state["cycle_key"] == same_state["cycle_key"]
    assert state["mode"] == {"requested": "normal", "effective": "normal"}
    assert state["selection"] == {"requested": "auto", "effective": "stable"}
    assert state["deslop"] == {"tracked": True, "status": "tracked"}
    assert state["validation"]["review_green"] == "unknown"
    assert state["validation"]["full_suite"] == "unknown"
    assert emergency["identity"]["branch"] is None
    assert emergency["deslop"] == {"tracked": False, "status": "skipped-emergency"}


def test_mark_deslop_closed_disables_tracked_sidecar_and_leaves_emergency_untracked(tmp_path: Path) -> None:
    tracked = _cycle(tmp_path)
    closed = mark_deslop_closed(tracked)
    closed_again = mark_deslop_closed(closed)

    assert closed["deslop"] == {"tracked": False, "status": "closed"}
    assert closed_again == closed
    assert tracked["deslop"] == {"tracked": True, "status": "tracked"}

    emergency = create_cycle(
        cwd=tmp_path / "repo",
        base="main",
        branch="HEAD",
        head="head-1",
        merge_base="base-1",
        requested_mode="emergency",
        effective_mode="emergency",
        selection="stable",
    )

    assert mark_deslop_closed(emergency)["deslop"] == {"tracked": False, "status": "skipped-emergency"}


def test_mark_deslop_closed_resumes_after_failed_sidecar(tmp_path: Path) -> None:
    failed = mark_deslop_failed(_cycle(tmp_path), command="review-deslop", returncode=2, reason="deslop failed")

    closed = mark_deslop_closed(failed)

    assert failed["stage"] == STAGE_RETRY_REQUESTED
    assert failed["pending_action"] == {"kind": "run-deslop"}
    assert closed["deslop"]["tracked"] is False
    assert closed["deslop"]["status"] == "closed"
    assert closed["stage"] == STAGE_CREATED
    assert closed["pending_action"] == {"kind": "resume-after-deslop"}
    assert closed["recovery"] == {"status": "none", "retry_count": 1}


def test_wait_states_are_idle_and_transitions_are_idempotent(tmp_path: Path) -> None:
    state = _cycle(tmp_path)
    original = json.loads(json.dumps(state))

    running = mark_running(state, round_id="round-1", lane="review_t1")
    running_again = mark_running(running, round_id="round-1", lane="review_t1")
    pending = mark_decision_pending(
        running_again,
        round_id="round-1",
        lane="review_t1",
        pending_action={"kind": "reprint-decision", "round_id": "round-1", "commands": ["clean", "findings"]},
    )
    findings = record_findings_decision(pending, round_id="round-1", lane="review_t1")
    findings_again = record_findings_decision(findings, round_id="round-1", lane="review_t1")
    blocked = mark_blocked(findings_again, reason="reviewer unavailable", round_id="round-1")
    crashed = mark_crashed(findings_again, reason="transport error", round_id="round-1")

    assert state == original
    assert no_work_stage_is_idle(running)
    assert no_work_stage_is_idle(pending)
    assert no_work_stage_is_idle(findings)
    assert no_work_stage_is_idle(blocked)
    assert no_work_stage_is_idle(crashed)
    assert len(running_again["rounds"]) == 1
    assert pending["pending_action"] == {"kind": "reprint-decision", "round_id": "round-1", "commands": ["clean", "findings"]}
    assert len(findings_again["decisions"]) == 1
    assert findings_again["stage"] == STAGE_FIX_PENDING
    assert findings_again["pending_action"] is None

    retry = mark_retry_requested(crashed, reason="operator retry")
    dismissed = dismiss_recovery(crashed, reason="ignore stale crash")
    aborted = abort_cycle(findings_again, reason="operator abort")

    assert retry["recovery"]["status"] == "retry-requested"
    assert retry["pending_action"] is None
    assert dismissed["recovery"]["status"] == "dismissed"
    assert dismissed["pending_action"] is None
    assert aborted["recovery"]["status"] == "aborted"
    assert aborted["pending_action"] is None


def test_clean_profile_steps_record_progress_until_final_review_green(tmp_path: Path) -> None:
    state = _cycle(tmp_path)
    state["review_plan"] = {
        "steps": [
            {"name": "broad-discovery", "count": 1, "model": "gpt-5.4", "reasoning_effort": "medium"},
            {"name": "precision-signoff", "count": 1, "model": "gpt-5.5", "reasoning_effort": "medium"},
        ]
    }
    first_pending = mark_review_step_pending(
        state,
        round_id="round-1",
        lane="review_t1",
        step_index=0,
        step_name="broad-discovery",
        reviewed_head="head-1",
    )

    assert first_pending["review_progress"]["current_step"] == {
        "index": 0,
        "name": "broad-discovery",
        "round_id": "round-1",
        "lane": "review_t1",
    }
    assert first_pending["rounds"][0]["profile_step"] == {"index": 0, "name": "broad-discovery"}

    queued = record_clean_decision(first_pending, round_id="round-1", lane="review_t1", reviewed_head="head-1")

    assert queued["stage"] == "created"
    assert queued["pending_action"] == {"kind": "run-review-step", "step_index": 1, "step": "precision-signoff"}
    assert queued["review_progress"]["current_step"] is None
    assert queued["review_progress"]["next_step_index"] == 1
    assert queued["review_progress"]["completed_steps"] == [
        {
            "index": 0,
            "name": "broad-discovery",
            "round_id": "round-1",
            "lane": "review_t1",
            "reviewed_head": "head-1",
        }
    ]

    second_pending = mark_review_step_pending(
        queued,
        round_id="round-2",
        lane="review_t1",
        step_index=1,
        step_name="precision-signoff",
        reviewed_head="head-1",
    )
    green = record_clean_decision(second_pending, round_id="round-2", lane="review_t1", reviewed_head="head-1")

    assert green["stage"] == STAGE_REVIEW_GREEN
    assert green["pending_action"] is None
    assert green["review_progress"]["next_step_index"] == 2
    assert [item["round_id"] for item in green["review_progress"]["completed_steps"]] == ["round-1", "round-2"]


def test_clean_discovery_skips_remaining_discovery_loops_to_signoff(tmp_path: Path) -> None:
    state = _cycle(tmp_path)
    state["review_plan"] = {
        "steps": [
            {"name": "broad-discovery-1", "count": 1, "model": "gpt-5.4", "reasoning_effort": "medium"},
            {"name": "broad-discovery-2", "count": 1, "model": "gpt-5.4", "reasoning_effort": "medium"},
            {"name": "broad-discovery-3", "count": 1, "model": "gpt-5.4", "reasoning_effort": "medium"},
            {"name": "precision-signoff", "count": 1, "model": "gpt-5.5", "reasoning_effort": "medium"},
        ]
    }
    pending = mark_review_step_pending(
        state,
        round_id="round-1",
        lane="review_t1",
        step_index=0,
        step_name="broad-discovery-1",
        reviewed_head="head-1",
    )

    clean = record_clean_decision(pending, round_id="round-1", lane="review_t1", reviewed_head="head-1")

    assert clean["stage"] == STAGE_CREATED
    assert clean["pending_action"] == {"kind": "run-review-step", "step_index": 3, "step": "precision-signoff"}
    assert clean["review_progress"]["next_step_index"] == 3
    assert [item["name"] for item in clean["review_progress"]["completed_steps"]] == ["broad-discovery-1"]


def test_clean_arena_round_still_runs_fixed_discovery_safety_pass(tmp_path: Path) -> None:
    state = _cycle(tmp_path)
    state["review_plan"] = {
        "steps": [
            {"name": "arena-discovery", "kind": "arena", "lane": "review_t1", "task_class": "phase_review"},
            {"name": "broad-discovery", "count": 1, "model": "gpt-5.4", "reasoning_effort": "medium"},
            {"name": "precision-signoff", "count": 1, "model": "gpt-5.5", "reasoning_effort": "medium"},
        ]
    }
    pending = mark_review_step_pending(
        state,
        round_id="round-1",
        lane="review_t1",
        step_index=0,
        step_name="arena-discovery",
        reviewed_head="head-1",
        grading_required=True,
        arena_round=True,
    )

    clean = record_clean_decision(pending, round_id="round-1", lane="review_t1", reviewed_head="head-1")

    assert clean["pending_action"] == {"kind": "run-review-step", "step_index": 1, "step": "broad-discovery"}
    assert clean["review_progress"]["next_step_index"] == 1


def test_clean_gate_profile_step_records_pending_signoff_and_completes(tmp_path: Path) -> None:
    state = _cycle(tmp_path)
    state["review_plan"] = {
        "steps": [
            {"name": "precision", "count": 1, "model": "gpt-5.5", "reasoning_effort": "medium"},
            {"name": "local-signoff", "kind": "gate", "gate": "phase_gate"},
        ]
    }
    review_pending = mark_review_step_pending(
        state,
        round_id="round-1",
        lane="review_t1",
        step_index=0,
        step_name="precision",
        reviewed_head="head-1",
    )
    gate_queued = record_clean_decision(review_pending, round_id="round-1", lane="review_t1", reviewed_head="head-1")

    assert gate_queued["stage"] == STAGE_CREATED
    assert gate_queued["pending_action"] == {
        "kind": "run-review-step",
        "step_index": 1,
        "step": "local-signoff",
        "step_kind": "gate",
        "gate": "phase_gate",
    }

    gate_pending = mark_gate_step_pending(
        gate_queued,
        round_id="gate-1",
        lane="review_t2",
        gate="phase_gate",
        step_index=1,
        step_name="local-signoff",
        reviewed_head="head-1",
    )

    assert gate_pending["stage"] == "decision-pending"
    assert gate_pending["pending_action"]["gate"] == "phase_gate"
    assert gate_pending["rounds"][1]["kind"] == "gate"
    assert gate_pending["rounds"][1]["gate"] == "phase_gate"

    green = record_clean_decision(gate_pending, round_id="gate-1", lane="review_t2", gate="phase_gate", reviewed_head="head-1")

    assert green["stage"] == STAGE_REVIEW_GREEN
    assert green["validation"]["review_green"] == "passed"
    assert green["review_progress"]["completed_steps"][-1] == {
        "index": 1,
        "name": "local-signoff",
        "round_id": "gate-1",
        "lane": "review_t2",
        "kind": "gate",
        "gate": "phase_gate",
        "reviewed_head": "head-1",
    }


def test_followup_clean_completes_source_profile_step_and_continues(tmp_path: Path) -> None:
    state = _cycle(tmp_path, mode="deep")
    state["review_plan"] = {
        "steps": [
            {"name": "broad-discovery", "count": 1, "model": "gpt-5.4", "reasoning_effort": "medium"},
            {"name": "precision-signoff", "count": 1, "model": "gpt-5.5", "reasoning_effort": "medium"},
        ]
    }
    pending = mark_review_step_pending(
        state,
        round_id="round-1",
        lane="review_t1",
        step_index=0,
        step_name="broad-discovery",
        reviewed_head="head-1",
    )
    findings = record_findings_decision(pending, round_id="round-1", lane="review_t1", reviewed_head="head-1")
    fixed = mark_fix_detected(findings, head="head-2")
    followup_pending = mark_followup_review_pending(
        fixed,
        round_id="followup-1",
        reviewed_head="head-2",
        source_round_id="round-1",
    )
    clean = record_followup_clean(followup_pending, round_id="followup-1", reviewed_head="head-2")

    assert clean["stage"] == STAGE_CREATED
    assert clean["active_findings"] is None
    assert clean["validation"]["review_green"] == "unknown"
    assert clean["pending_action"] == {"kind": "run-review-step", "step_index": 1, "step": "precision-signoff"}
    assert clean["review_progress"]["completed_steps"] == [
        {
            "index": 0,
            "name": "broad-discovery",
            "round_id": "round-1",
            "lane": "review_t1",
            "reviewed_head": "head-2",
        }
    ]


def test_followup_clean_after_discovery_findings_keeps_discovery_budget(tmp_path: Path) -> None:
    state = _cycle(tmp_path, mode="deep")
    state["review_plan"] = {
        "steps": [
            {"name": "broad-discovery-1", "count": 1, "model": "gpt-5.4", "reasoning_effort": "medium"},
            {"name": "broad-discovery-2", "count": 1, "model": "gpt-5.4", "reasoning_effort": "medium"},
            {"name": "precision-signoff", "count": 1, "model": "gpt-5.5", "reasoning_effort": "medium"},
        ]
    }
    pending = mark_review_step_pending(
        state,
        round_id="round-1",
        lane="review_t1",
        step_index=0,
        step_name="broad-discovery-1",
        reviewed_head="head-1",
    )
    findings = record_findings_decision(pending, round_id="round-1", lane="review_t1", reviewed_head="head-1")
    fixed = mark_fix_detected(findings, head="head-2")
    followup_pending = mark_followup_review_pending(
        fixed,
        round_id="followup-1",
        reviewed_head="head-2",
        source_round_id="round-1",
    )

    clean = record_followup_clean(followup_pending, round_id="followup-1", reviewed_head="head-2")

    assert clean["pending_action"] == {"kind": "run-review-step", "step_index": 1, "step": "broad-discovery-2"}
    assert clean["review_progress"]["next_step_index"] == 1


def test_followup_clean_reruns_sticky_signoff_step(tmp_path: Path) -> None:
    state = _cycle(tmp_path, mode="deep")
    state["review_plan"] = {
        "steps": [
            {
                "name": "precision-signoff",
                "count": 1,
                "model": "gpt-5.5",
                "reasoning_effort": "medium",
                "rerun_on_findings": True,
            },
        ]
    }
    pending = mark_review_step_pending(
        state,
        round_id="signoff-1",
        lane="review_t1",
        step_index=0,
        step_name="precision-signoff",
        reviewed_head="head-1",
    )
    findings = record_findings_decision(pending, round_id="signoff-1", lane="review_t1", reviewed_head="head-1")
    fixed = mark_fix_detected(findings, head="head-2")
    followup_pending = mark_followup_review_pending(
        fixed,
        round_id="followup-1",
        reviewed_head="head-2",
        source_round_id="signoff-1",
    )
    clean = record_followup_clean(followup_pending, round_id="followup-1", reviewed_head="head-2")

    assert clean["stage"] == STAGE_CREATED
    assert clean["active_findings"] is None
    assert clean["validation"]["review_green"] == "unknown"
    assert clean["pending_action"] == {"kind": "run-review-step", "step_index": 0, "step": "precision-signoff"}
    assert clean["review_progress"]["next_step_index"] == 0
    assert clean["review_progress"]["completed_steps"] == []

    rerun_pending = mark_review_step_pending(
        clean,
        round_id="signoff-2",
        lane="review_t1",
        step_index=0,
        step_name="precision-signoff",
        reviewed_head="head-2",
    )
    green = record_clean_decision(rerun_pending, round_id="signoff-2", lane="review_t1", reviewed_head="head-2")

    assert green["stage"] == STAGE_REVIEW_GREEN
    assert green["review_progress"]["completed_steps"] == [
        {
            "index": 0,
            "name": "precision-signoff",
            "round_id": "signoff-2",
            "lane": "review_t1",
            "reviewed_head": "head-2",
        }
    ]


def test_emergency_terminal_findings_get_one_verification_round(tmp_path: Path) -> None:
    state = _cycle(tmp_path)
    state["review_plan"] = {
        "steps": [
            {
                "name": "urgent-signoff",
                "count": 1,
                "model": "gpt-5.5",
                "reasoning_effort": "medium",
                "max_review_rounds": 2,
            },
        ]
    }
    pending = mark_review_step_pending(
        state,
        round_id="signoff-1",
        lane="review_t1",
        step_index=0,
        step_name="urgent-signoff",
        reviewed_head="head-1",
    )
    findings = record_findings_decision(pending, round_id="signoff-1", lane="review_t1", reviewed_head="head-1")

    fixed = mark_fix_detected(findings, head="head-2")

    assert fixed["stage"] == STAGE_CREATED
    assert fixed["active_findings"] is None
    assert fixed["validation"]["review_green"] == "unknown"
    assert fixed["pending_action"] == {
        "kind": "run-review-step",
        "step_index": 0,
        "step": "urgent-signoff",
        "fix_verification": {
            "source_round_id": "signoff-1",
            "source_lane": "review_t1",
            "findings_reviewed_head": "head-1",
            "fix_head": "head-2",
        },
    }
    assert fixed["review_progress"]["next_step_index"] == 0
    assert fixed["review_progress"]["completed_steps"] == []
    assert fixed["review_heads"]["last_fix_head"] == "head-2"


def test_emergency_terminal_findings_stop_after_round_budget(tmp_path: Path) -> None:
    state = _cycle(tmp_path, mode="emergency")
    state["review_plan"] = {
        "steps": [
            {
                "name": "urgent-signoff",
                "count": 1,
                "model": "gpt-5.5",
                "reasoning_effort": "medium",
                "max_review_rounds": 2,
            },
        ]
    }
    first_pending = mark_review_step_pending(
        state,
        round_id="signoff-1",
        lane="review_t1",
        step_index=0,
        step_name="urgent-signoff",
        reviewed_head="head-1",
    )
    first_findings = record_findings_decision(
        first_pending,
        round_id="signoff-1",
        lane="review_t1",
        reviewed_head="head-1",
    )
    first_fixed = mark_fix_detected(first_findings, head="head-2")
    second_pending = mark_review_step_pending(
        first_fixed,
        round_id="signoff-2",
        lane="review_t1",
        step_index=0,
        step_name="urgent-signoff",
        reviewed_head="head-2",
        post_findings_rerun=True,
        fix_verification=first_fixed["pending_action"]["fix_verification"],
    )
    second_findings = record_findings_decision(
        second_pending,
        round_id="signoff-2",
        lane="review_t1",
        reviewed_head="head-2",
    )

    exhausted = mark_fix_detected(second_findings, head="head-3")

    assert exhausted["stage"] == STAGE_FIX_PENDING
    assert exhausted["validation"]["review_green"] == "failed"
    assert exhausted["pending_action"] == {
        "kind": "review-round-budget-exhausted",
        "round_id": "signoff-2",
        "lane": "review_t1",
        "step_index": 0,
        "step": "urgent-signoff",
        "max_review_rounds": 2,
        "fix_verification": {
            "source_round_id": "signoff-2",
            "source_lane": "review_t1",
            "findings_reviewed_head": "head-2",
            "fix_head": "head-3",
        },
    }
    assert exhausted["active_findings"]["status"] == "review-round-budget-exhausted"
    assert exhausted["active_findings"]["fix_head"] == "head-3"
    assert [item["round_id"] for item in exhausted["rounds"]] == ["signoff-1", "signoff-2"]


def test_non_deep_discovery_findings_advance_without_extra_discovery(tmp_path: Path) -> None:
    state = _cycle(tmp_path)
    state["review_plan"] = {
        "steps": [
            {"name": "broad-discovery", "count": 1, "model": "gpt-5.4", "reasoning_effort": "medium"},
            {"name": "precision-signoff", "count": 1, "model": "gpt-5.5", "reasoning_effort": "medium"},
        ]
    }
    pending = mark_review_step_pending(
        state,
        round_id="round-1",
        lane="review_t1",
        step_index=0,
        step_name="broad-discovery",
        reviewed_head="head-1",
    )
    findings = record_findings_decision(pending, round_id="round-1", lane="review_t1", reviewed_head="head-1")

    fixed = mark_fix_detected(findings, head="head-2")

    assert fixed["stage"] == STAGE_CREATED
    assert fixed["active_findings"] is None
    assert fixed["validation"]["review_green"] == "unknown"
    assert fixed["pending_action"] == {
        "kind": "run-review-step",
        "step_index": 1,
        "step": "precision-signoff",
        "fix_verification": {
            "source_round_id": "round-1",
            "source_lane": "review_t1",
            "findings_reviewed_head": "head-1",
            "fix_head": "head-2",
        },
    }
    assert fixed["review_progress"]["next_step_index"] == 1
    assert fixed["review_progress"]["completed_steps"] == [
        {
            "index": 0,
            "name": "broad-discovery",
            "round_id": "round-1",
            "lane": "review_t1",
            "reviewed_head": "head-1",
        }
    ]
    assert fixed["review_heads"]["last_fix_head"] == "head-2"


def test_non_deep_discovery_findings_consume_one_discovery_budget(tmp_path: Path) -> None:
    state = _cycle(tmp_path)
    state["review_plan"] = {
        "steps": [
            {"name": "broad-discovery-1", "count": 1, "model": "gpt-5.4", "reasoning_effort": "medium"},
            {"name": "broad-discovery-2", "count": 1, "model": "gpt-5.4", "reasoning_effort": "medium"},
            {"name": "precision-signoff", "count": 1, "model": "gpt-5.5", "reasoning_effort": "medium"},
        ]
    }
    pending = mark_review_step_pending(
        state,
        round_id="round-1",
        lane="review_t1",
        step_index=0,
        step_name="broad-discovery-1",
        reviewed_head="head-1",
    )
    findings = record_findings_decision(pending, round_id="round-1", lane="review_t1", reviewed_head="head-1")

    fixed = mark_fix_detected(findings, head="head-2")

    assert fixed["stage"] == STAGE_CREATED
    assert fixed["pending_action"] == {
        "kind": "run-review-step",
        "step_index": 1,
        "step": "broad-discovery-2",
        "fix_verification": {
            "source_round_id": "round-1",
            "source_lane": "review_t1",
            "findings_reviewed_head": "head-1",
            "fix_head": "head-2",
        },
    }
    assert fixed["review_progress"]["completed_steps"] == [
        {
            "index": 0,
            "name": "broad-discovery-1",
            "round_id": "round-1",
            "lane": "review_t1",
            "reviewed_head": "head-1",
        }
    ]


def test_non_deep_arena_findings_consume_one_arena_budget(tmp_path: Path) -> None:
    state = _cycle(tmp_path)
    state["review_plan"] = {
        "steps": [
            {"name": "arena-discovery", "kind": "arena", "lane": "review_t3", "task_class": "phase_review"},
            {"name": "broad-discovery", "count": 1, "model": "gpt-5.4", "reasoning_effort": "medium"},
            {"name": "precision-signoff", "count": 1, "model": "gpt-5.5", "reasoning_effort": "medium"},
        ]
    }
    pending = mark_review_step_pending(
        state,
        round_id="arena-1",
        lane="review_t3",
        step_index=0,
        step_name="arena-discovery",
        reviewed_head="head-1",
        grading_required=True,
        arena_round=True,
    )
    findings = record_findings_decision(pending, round_id="arena-1", lane="review_t3", reviewed_head="head-1")

    fixed = mark_fix_detected(findings, head="head-2")

    assert fixed["stage"] == STAGE_CREATED
    assert fixed["pending_action"] == {
        "kind": "run-review-step",
        "step_index": 1,
        "step": "broad-discovery",
        "fix_verification": {
            "source_round_id": "arena-1",
            "source_lane": "review_t3",
            "findings_reviewed_head": "head-1",
            "fix_head": "head-2",
        },
    }
    assert fixed["review_progress"]["completed_steps"] == [
        {
            "index": 0,
            "name": "arena-discovery",
            "round_id": "arena-1",
            "lane": "review_t3",
            "reviewed_head": "head-1",
            "arena_round": True,
        }
    ]


def test_non_deep_signoff_findings_rerun_until_clean(tmp_path: Path) -> None:
    state = _cycle(tmp_path)
    state["review_plan"] = {
        "steps": [
            {
                "name": "precision-signoff",
                "count": 1,
                "model": "gpt-5.5",
                "reasoning_effort": "medium",
                "rerun_on_findings": True,
            },
            {"name": "post-check", "count": 1, "model": "gpt-5.5", "reasoning_effort": "medium"},
        ]
    }
    pending = mark_review_step_pending(
        state,
        round_id="signoff-1",
        lane="review_t1",
        step_index=0,
        step_name="precision-signoff",
        reviewed_head="head-1",
    )
    findings = record_findings_decision(pending, round_id="signoff-1", lane="review_t1", reviewed_head="head-1")

    fixed = mark_fix_detected(findings, head="head-2")

    assert fixed["stage"] == STAGE_CREATED
    assert fixed["pending_action"] == {
        "kind": "run-review-step",
        "step_index": 0,
        "step": "precision-signoff",
        "fix_verification": {
            "source_round_id": "signoff-1",
            "source_lane": "review_t1",
            "findings_reviewed_head": "head-1",
            "fix_head": "head-2",
        },
    }
    assert fixed["review_progress"]["next_step_index"] == 0
    assert fixed["review_progress"]["completed_steps"] == []


def test_existing_post_findings_discovery_rerun_clean_preserves_remaining_discovery(tmp_path: Path) -> None:
    state = _cycle(tmp_path)
    state["review_plan"] = {
        "steps": [
            {"name": "broad-discovery-1", "count": 1, "model": "gpt-5.4", "reasoning_effort": "medium"},
            {"name": "broad-discovery-2", "count": 1, "model": "gpt-5.4", "reasoning_effort": "medium"},
            {"name": "precision-signoff", "count": 1, "model": "gpt-5.5", "reasoning_effort": "medium"},
        ]
    }
    pending = mark_review_step_pending(
        state,
        round_id="round-1",
        lane="review_t1",
        step_index=0,
        step_name="broad-discovery-1",
        reviewed_head="head-1",
    )
    findings = record_findings_decision(pending, round_id="round-1", lane="review_t1", reviewed_head="head-1")
    fixed = json.loads(json.dumps(findings))
    fixed["stage"] = STAGE_CREATED
    fixed["active_findings"] = None
    fixed["validation"]["review_green"] = "unknown"
    fixed["review_progress"]["next_step_index"] = 0
    fixed["review_progress"]["next_step_name"] = "broad-discovery-1"
    fixed["review_progress"]["current_step"] = None
    fixed["pending_action"] = {
        "kind": "run-review-step",
        "step_index": 0,
        "step": "broad-discovery-1",
        "fix_verification": {
            "source_round_id": "round-1",
            "source_lane": "review_t1",
            "findings_reviewed_head": "head-1",
            "fix_head": "head-2",
        },
    }
    running_rerun = mark_review_step_pending(
        fixed,
        round_id="round-2",
        lane="review_t1",
        step_index=0,
        step_name="broad-discovery-1",
        reviewed_head="head-2",
        post_findings_rerun=True,
    )

    clean = record_clean_decision(running_rerun, round_id="round-2", lane="review_t1", reviewed_head="head-2")

    assert clean["stage"] == STAGE_CREATED
    assert clean["pending_action"] == {"kind": "run-review-step", "step_index": 1, "step": "broad-discovery-2"}
    assert clean["review_progress"]["completed_steps"] == [
        {
            "index": 0,
            "name": "broad-discovery-1",
            "round_id": "round-2",
            "lane": "review_t1",
            "reviewed_head": "head-2",
        }
    ]


def test_github_findings_followup_reruns_last_local_signoff_step(tmp_path: Path) -> None:
    state = _cycle(tmp_path, mode="deep")
    state["review_plan"] = {
        "steps": [
            {
                "name": "precision-signoff",
                "count": 1,
                "model": "gpt-5.5",
                "reasoning_effort": "medium",
                "rerun_on_findings": True,
            },
        ]
    }
    pending = mark_review_step_pending(
        state,
        round_id="signoff-1",
        lane="review_t1",
        step_index=0,
        step_name="precision-signoff",
        reviewed_head="head-1",
    )
    green = record_clean_decision(pending, round_id="signoff-1", lane="review_t1", reviewed_head="head-1")

    github_findings = record_github_result(
        green,
        result="findings",
        note="GitHub found a boundary case.",
        reviewed_head="head-1",
    )

    assert github_findings["stage"] == STAGE_FIX_PENDING
    assert github_findings["validation"]["review_green"] == "unknown"
    assert github_findings["github_review"]["status"] == "findings"
    assert github_findings["active_findings"] == {
        "round_id": "github-review-1",
        "lane": "review-github",
        "reviewed_head": "head-1",
        "status": STAGE_FIX_PENDING,
        "profile_round_id": "signoff-1",
        "rerun_profile_round": True,
        "note": "GitHub found a boundary case.",
    }

    fixed = mark_fix_detected(github_findings, head="head-2")
    followup_pending = mark_followup_review_pending(
        fixed,
        round_id="followup-1",
        reviewed_head="head-2",
        source_round_id="github-review-1",
    )
    clean = record_followup_clean(followup_pending, round_id="followup-1", reviewed_head="head-2")

    assert clean["stage"] == STAGE_CREATED
    assert clean["active_findings"] is None
    assert clean["pending_action"] == {"kind": "run-review-step", "step_index": 0, "step": "precision-signoff"}
    assert clean["review_progress"]["completed_steps"] == []


def test_github_result_defaults_to_latest_local_reviewed_head(tmp_path: Path) -> None:
    state = _cycle(tmp_path)
    state["review_plan"] = {
        "steps": [
            {
                "name": "precision-signoff",
                "count": 1,
                "model": "gpt-5.5",
                "reasoning_effort": "medium",
                "rerun_on_findings": True,
            },
        ]
    }
    pending = mark_review_step_pending(
        state,
        round_id="signoff-1",
        lane="review_t1",
        step_index=0,
        step_name="precision-signoff",
        reviewed_head="head-2",
    )
    green = record_clean_decision(pending, round_id="signoff-1", lane="review_t1", reviewed_head="head-2")

    github_findings = record_github_result(green, result="findings")

    assert github_findings["github_review"]["reviewed_head"] == "head-2"
    assert github_findings["active_findings"]["reviewed_head"] == "head-2"


def test_github_findings_force_signoff_rerun_for_non_sticky_profile(tmp_path: Path) -> None:
    state = _cycle(tmp_path, mode="deep")
    state["review_plan"] = {
        "steps": [
            {
                "name": "custom-signoff",
                "count": 1,
                "model": "gpt-5.5",
                "reasoning_effort": "medium",
            },
        ]
    }
    pending = mark_review_step_pending(
        state,
        round_id="signoff-1",
        lane="review_t1",
        step_index=0,
        step_name="custom-signoff",
        reviewed_head="head-1",
    )
    green = record_clean_decision(pending, round_id="signoff-1", lane="review_t1", reviewed_head="head-1")
    github_findings = record_github_result(green, result="findings", reviewed_head="head-1")
    fixed = mark_fix_detected(github_findings, head="head-2")
    followup_pending = mark_followup_review_pending(
        fixed,
        round_id="followup-1",
        reviewed_head="head-2",
        source_round_id="github-review-1",
    )

    clean = record_followup_clean(followup_pending, round_id="followup-1", reviewed_head="head-2")

    assert clean["stage"] == STAGE_CREATED
    assert clean["pending_action"] == {"kind": "run-review-step", "step_index": 0, "step": "custom-signoff"}
    assert clean["review_progress"]["completed_steps"] == []


def test_github_findings_anchor_to_latest_completed_step_not_older_sticky_step(tmp_path: Path) -> None:
    state = _cycle(tmp_path, mode="deep")
    state["review_plan"] = {
        "steps": [
            {
                "name": "early-sticky",
                "count": 1,
                "model": "gpt-5.5",
                "reasoning_effort": "medium",
                "rerun_on_findings": True,
            },
            {
                "name": "final-signoff",
                "count": 1,
                "model": "gpt-5.5",
                "reasoning_effort": "medium",
            },
        ]
    }
    first_pending = mark_review_step_pending(
        state,
        round_id="early-1",
        lane="review_t1",
        step_index=0,
        step_name="early-sticky",
        reviewed_head="head-1",
    )
    queued = record_clean_decision(first_pending, round_id="early-1", lane="review_t1", reviewed_head="head-1")
    final_pending = mark_review_step_pending(
        queued,
        round_id="final-1",
        lane="review_t1",
        step_index=1,
        step_name="final-signoff",
        reviewed_head="head-1",
    )
    green = record_clean_decision(final_pending, round_id="final-1", lane="review_t1", reviewed_head="head-1")

    github_findings = record_github_result(green, result="findings", reviewed_head="head-1")

    assert github_findings["active_findings"]["profile_round_id"] == "final-1"
    fixed = mark_fix_detected(github_findings, head="head-2")
    followup_pending = mark_followup_review_pending(
        fixed,
        round_id="followup-1",
        reviewed_head="head-2",
        source_round_id="github-review-1",
    )
    clean = record_followup_clean(followup_pending, round_id="followup-1", reviewed_head="head-2")

    assert clean["pending_action"] == {"kind": "run-review-step", "step_index": 1, "step": "final-signoff"}
    assert clean["review_progress"]["completed_steps"] == [
        {
            "index": 0,
            "name": "early-sticky",
            "round_id": "early-1",
            "lane": "review_t1",
            "reviewed_head": "head-1",
        }
    ]


def test_gate_findings_require_fix_followup_clean_and_same_gate_rerun(tmp_path: Path) -> None:
    state = _cycle(tmp_path, mode="deep")
    pending = mark_decision_pending(state, round_id="gate-1", lane="review_t2", reviewed_head="head-1")
    findings = record_findings_decision(pending, round_id="gate-1", lane="review_t2", reviewed_head="head-1")

    assert findings["stage"] == STAGE_FIX_PENDING
    assert findings["pending_action"] is None
    assert not can_advance_or_anchor(findings)
    with pytest.raises(ValueError, match="local-green handoff"):
        mark_local_green_handoff(findings)

    fixed = mark_fix_detected(findings, head="head-2")
    assert fixed["pending_action"]["kind"] == "run-followup"

    followup_clean = record_followup_clean(fixed, round_id="followup-1", reviewed_head="head-2")
    assert followup_clean["stage"] == STAGE_GATE_RERUN_NEEDED
    assert followup_clean["pending_action"]["kind"] == "rerun-gate"
    assert followup_clean["pending_action"]["lane"] == "review_t2"
    assert not can_advance_or_anchor(followup_clean)
    with pytest.raises(ValueError, match="same gate"):
        record_clean_decision(followup_clean, round_id="wrong-gate", lane="review_t4", reviewed_head="head-2")
    with pytest.raises(ValueError, match="local-green handoff"):
        mark_local_green_handoff(followup_clean)

    rerun_pending = mark_gate_step_pending(
        followup_clean,
        round_id="gate-2",
        lane="review_t2",
        gate="review_t2",
        step_index=0,
        step_name="review_t2",
        reviewed_head="head-2",
    )
    rerun_clean = record_clean_decision(rerun_pending, round_id="gate-2", lane="review_t2", reviewed_head="head-2")
    assert rerun_clean["stage"] == STAGE_REVIEW_GREEN
    assert rerun_clean["active_findings"] is None
    assert rerun_clean["pending_action"] is None
    assert rerun_clean["validation"]["review_green"] == "passed"
    assert rerun_clean["validation"]["full_suite"] == "unknown"
    assert rerun_clean["validation"]["ci"] == "unknown"
    assert rerun_clean["resolved_gate_findings"] == [
        {
            "source_round_id": "gate-1",
            "followup_round_id": "followup-1",
            "rerun_round_id": "gate-2",
            "lane": "review_t2",
            "gate": "review_t2",
            "resolved_head": "head-2",
        }
    ]
    assert can_advance_or_anchor(rerun_clean)

    handoff = mark_local_green_handoff(rerun_clean, focused="passed", full_suite="classified", ci="classified")
    assert handoff["stage"] == STAGE_LOCAL_GREEN_HANDOFF
    assert handoff["validation"]["focused"] == "passed"
    assert handoff["validation"]["full_suite"] == "classified"
    assert handoff["validation"]["ci"] == "classified"


def test_non_deep_gate_findings_rerun_same_gate_without_followup(tmp_path: Path) -> None:
    state = _cycle(tmp_path)
    pending = mark_decision_pending(state, round_id="gate-1", lane="review_t2", reviewed_head="head-1")
    findings = record_findings_decision(pending, round_id="gate-1", lane="review_t2", reviewed_head="head-1")

    fixed = mark_fix_detected(findings, head="head-2")

    assert fixed["stage"] == STAGE_GATE_RERUN_NEEDED
    assert fixed["pending_action"] == {
        "kind": "rerun-gate",
        "lane": "review_t2",
        "gate": "review_t2",
        "source_round_id": "gate-1",
        "head": "head-2",
        "fix_verification": {
            "source_round_id": "gate-1",
            "source_lane": "review_t2",
            "findings_reviewed_head": "head-1",
            "fix_head": "head-2",
        },
    }
    assert fixed["active_findings"]["status"] == STAGE_GATE_RERUN_NEEDED
    assert fixed["review_heads"]["last_fix_head"] == "head-2"

    rerun_pending = mark_gate_step_pending(
        fixed,
        round_id="gate-2",
        lane="review_t2",
        gate="review_t2",
        step_index=0,
        step_name="review_t2",
        reviewed_head="head-2",
    )
    rerun_clean = record_clean_decision(rerun_pending, round_id="gate-2", lane="review_t2", reviewed_head="head-2")

    assert rerun_clean["stage"] == STAGE_REVIEW_GREEN
    assert rerun_clean["active_findings"] is None
    assert rerun_clean["resolved_gate_findings"] == [
        {
            "source_round_id": "gate-1",
            "rerun_round_id": "gate-2",
            "lane": "review_t2",
            "gate": "review_t2",
            "resolved_head": "head-2",
        }
    ]


def test_gate_profile_findings_require_same_gate_rerun_before_completion(tmp_path: Path) -> None:
    state = _cycle(tmp_path, mode="deep")
    state["review_plan"] = {"steps": [{"name": "local-signoff", "kind": "gate", "gate": "phase_gate"}]}
    pending = mark_gate_step_pending(
        state,
        round_id="gate-1",
        lane="review_t2",
        gate="phase_gate",
        step_index=0,
        step_name="local-signoff",
        reviewed_head="head-1",
    )
    findings = record_findings_decision(pending, round_id="gate-1", lane="review_t2", gate="phase_gate", reviewed_head="head-1")
    fixed = mark_fix_detected(findings, head="head-2")
    followup_clean = record_followup_clean(fixed, round_id="followup-1", reviewed_head="head-2")

    assert followup_clean["stage"] == STAGE_GATE_RERUN_NEEDED
    assert followup_clean["pending_action"]["gate"] == "phase_gate"
    assert followup_clean["pending_action"]["step"] == "local-signoff"
    with pytest.raises(ValueError, match="same gate"):
        record_clean_decision(followup_clean, round_id="gate-2", lane="review_t4", gate="pr_gate", reviewed_head="head-2")

    rerun_pending = mark_gate_step_pending(
        followup_clean,
        round_id="gate-2",
        lane="review_t2",
        gate="phase_gate",
        step_index=0,
        step_name="local-signoff",
        reviewed_head="head-2",
    )
    rerun_clean = record_clean_decision(rerun_pending, round_id="gate-2", lane="review_t2", gate="phase_gate", reviewed_head="head-2")

    assert rerun_clean["stage"] == STAGE_REVIEW_GREEN
    assert rerun_clean["active_findings"] is None
    assert rerun_clean["review_progress"]["completed_steps"] == [
        {
            "index": 0,
            "name": "local-signoff",
            "round_id": "gate-2",
            "lane": "review_t2",
            "kind": "gate",
            "gate": "phase_gate",
            "reviewed_head": "head-2",
        }
    ]


def test_followup_findings_preserve_original_gate_rerun_requirement(tmp_path: Path) -> None:
    state = _cycle(tmp_path, mode="deep")
    findings = record_findings_decision(state, round_id="gate-1", lane="review_t4", reviewed_head="head-1")
    fixed = mark_fix_detected(findings, head="head-2")
    followup_findings = record_followup_findings(fixed, round_id="followup-1", reviewed_head="head-2")

    assert followup_findings["stage"] == STAGE_FIX_PENDING
    assert followup_findings["active_findings"]["gate"]["round_id"] == "gate-1"

    fixed_again = mark_fix_detected(followup_findings, head="head-3")
    followup_clean = record_followup_clean(fixed_again, round_id="followup-2", reviewed_head="head-3")

    assert followup_clean["stage"] == STAGE_GATE_RERUN_NEEDED
    assert followup_clean["pending_action"]["lane"] == "review_t4"
    assert followup_clean["pending_action"]["source_round_id"] == "gate-1"
    assert not can_advance_or_anchor(followup_clean)
