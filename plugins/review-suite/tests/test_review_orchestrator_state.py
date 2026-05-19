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
    abort_cycle,
    can_advance_or_anchor,
    create_cycle,
    dismiss_recovery,
    mark_blocked,
    mark_crashed,
    mark_decision_pending,
    mark_fix_detected,
    mark_followup_review_pending,
    mark_local_green_handoff,
    mark_retry_requested,
    mark_running,
    mark_review_step_pending,
    no_work_stage_is_idle,
    record_clean_decision,
    record_findings_decision,
    record_followup_clean,
    record_followup_findings,
)


def _cycle(tmp_path: Path) -> dict[str, object]:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return create_cycle(
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


def test_followup_clean_completes_source_profile_step_and_continues(tmp_path: Path) -> None:
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


def test_gate_findings_require_fix_followup_clean_and_same_gate_rerun(tmp_path: Path) -> None:
    state = _cycle(tmp_path)
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

    rerun_clean = record_clean_decision(followup_clean, round_id="gate-2", lane="review_t2", reviewed_head="head-2")
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


def test_followup_findings_preserve_original_gate_rerun_requirement(tmp_path: Path) -> None:
    state = _cycle(tmp_path)
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
