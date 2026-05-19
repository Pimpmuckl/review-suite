from __future__ import annotations

import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from review_suite_core import orchestrator_runner
from review_suite_core.orchestrator_state import (
    STAGE_CREATED,
    STAGE_DECISION_PENDING,
    STAGE_REVIEW_GREEN,
    STAGE_RETRY_REQUESTED,
    create_cycle,
    mark_fix_detected,
    mark_review_step_pending,
    record_clean_decision,
    record_findings_decision,
)


def _cycle(tmp_path: Path, *, mode: str = "normal", deslop_enabled: bool = True, step_names: tuple[str, ...] = ("precision",)) -> dict[str, object]:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    state = create_cycle(
        cwd=repo,
        base="main",
        branch="feature/orchestrator",
        head="head-1",
        merge_base="base-1",
        requested_mode=mode,
        effective_mode=mode,
        selection="auto",
        effective_selection="stable",
        deslop_enabled=deslop_enabled,
    )
    state["review_plan"] = {
        "steps": [
            {
                "name": step_name,
                "count": 1,
                "model": "gpt-5.5",
                "reasoning_effort": "medium",
                "service_tier": None,
            }
            for step_name in step_names
        ]
    }
    return state


def _stub_review(monkeypatch, *round_ids: str) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    ids = list(round_ids) or ["phase_review-round-1"]

    def fake_run(**kwargs: object) -> dict[str, object]:
        calls.append(dict(kwargs))
        round_id = ids[min(len(calls) - 1, len(ids) - 1)]
        return {
            "round_id": round_id,
            "lane": "review_t1",
            "kind": "review",
            "status": "completed",
            "blocked": False,
            "reviewed_head": "head-1",
            "output_refs": ["rollout://thread/gpt-5.5-medium"],
            "runs": [
                {
                    "slot": "alpha",
                    "status": "completed",
                    "summary": "No findings.",
                    "ref": "rollout://thread/gpt-5.5-medium",
                    "blocked": False,
                    "block": None,
                }
            ],
            "round_state_dir": "state/orchestrator/review-rounds",
        }

    monkeypatch.setattr(orchestrator_runner, "run_review_step", fake_run)
    return calls


def _stub_followup(monkeypatch, *round_ids: str) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    ids = list(round_ids) or ["followup-round-1"]

    def fake_run(**kwargs: object) -> dict[str, object]:
        calls.append(dict(kwargs))
        round_id = ids[min(len(calls) - 1, len(ids) - 1)]
        scope = kwargs.get("review_scope")
        reviewed_head = str(scope.get("reviewed_head") if isinstance(scope, dict) else "head-2")
        return {
            "round_id": round_id,
            "lane": "review-followup",
            "kind": "followup",
            "status": "completed",
            "blocked": False,
            "reviewed_head": reviewed_head,
            "output_refs": [f"rollout://{round_id}/alpha"],
            "runs": [
                {
                    "slot": "alpha",
                    "status": "completed",
                    "summary": "No findings.",
                    "ref": f"rollout://{round_id}/alpha",
                    "blocked": False,
                    "block": None,
                }
            ],
            "round_state_dir": "state/orchestrator/review-rounds",
        }

    monkeypatch.setattr(orchestrator_runner, "run_followup_review_step", fake_run)
    return calls


def _stub_gate(monkeypatch, *round_ids: str) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    ids = list(round_ids) or ["phase_gate-round-1"]

    def fake_run(**kwargs: object) -> tuple[dict[str, object], int]:
        calls.append(dict(kwargs))
        round_id = ids[min(len(calls) - 1, len(ids) - 1)]
        return (
            {
                "round_id": round_id,
                "task": "review_t2",
                "status": "signoff_pending",
                "blocked": False,
                "signoff_required": True,
                "runs": [
                    {
                        "slot": "Alpha",
                        "status": "completed",
                        "summary": "No findings.",
                        "blocked": False,
                        "block": None,
                        "ref": f"rollout://{round_id}/alpha",
                    }
                ],
            },
            0,
        )

    monkeypatch.setattr(orchestrator_runner, "run_gate_step", fake_run)
    return calls


def test_runner_executes_one_deslop_step_and_marks_done(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[list[str], Path]] = []
    review_calls = _stub_review(monkeypatch)

    def fake_run(*, command: list[str], cwd: Path) -> subprocess.CompletedProcess:
        calls.append((command, cwd))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(orchestrator_runner, "run_deslop_subprocess", fake_run)

    result = orchestrator_runner.run_one_expensive_step(_cycle(tmp_path))

    assert result.ran_step is True
    assert result.step == "deslop"
    assert result.state["stage"] == STAGE_CREATED
    assert result.state["pending_action"] == {"kind": "resume-after-deslop"}
    assert result.state["deslop"]["status"] == "done"
    assert result.state["deslop"]["returncode"] == 0
    assert len(calls) == 1
    command, cwd = calls[0]
    assert Path(command[1]).name == "review_deslop.py"
    assert "--output-only" in command
    assert command[-2:] == ["--base", "main"]
    assert cwd == tmp_path / "repo"

    second = orchestrator_runner.run_one_expensive_step(result.state, state_dir=tmp_path / "state")

    assert second.ran_step is True
    assert second.step == "review"
    assert second.state["stage"] == STAGE_DECISION_PENDING
    assert second.state["pending_action"] == {
        "kind": "decision",
        "round_id": "phase_review-round-1",
        "lane": "review_t1",
        "step_index": 0,
        "step": "precision",
    }
    assert second.state["rounds"][0]["round_id"] == "phase_review-round-1"
    assert second.state["rounds"][0]["lane"] == "review_t1"
    assert second.state["rounds"][0]["kind"] == "review"
    assert second.state["rounds"][0]["review_status"] == "completed"
    assert second.state["rounds"][0]["output_refs"] == ["rollout://thread/gpt-5.5-medium"]
    assert len(calls) == 1
    assert len(review_calls) == 1

    third = orchestrator_runner.run_one_expensive_step(second.state, state_dir=tmp_path / "state")

    assert third.ran_step is False
    assert len(review_calls) == 1


def test_runner_walks_profile_steps_after_clean_decisions(monkeypatch, tmp_path: Path) -> None:
    review_calls = _stub_review(monkeypatch, "phase_review-round-1", "phase_review-round-2")
    state = _cycle(tmp_path, deslop_enabled=False, step_names=("broad-discovery", "precision-signoff"))

    first = orchestrator_runner.run_one_expensive_step(state, state_dir=tmp_path / "state")

    assert first.ran_step is True
    assert first.state["stage"] == STAGE_DECISION_PENDING
    assert first.state["pending_action"]["step_index"] == 0
    assert first.state["pending_action"]["step"] == "broad-discovery"
    assert first.state["review_progress"]["current_step"]["name"] == "broad-discovery"
    assert review_calls[0]["step_name"] == "broad-discovery"

    queued = record_clean_decision(first.state, round_id="phase_review-round-1", lane="review_t1", reviewed_head="head-1")

    assert queued["stage"] == STAGE_CREATED
    assert queued["pending_action"] == {"kind": "run-review-step", "step_index": 1, "step": "precision-signoff"}
    assert queued["review_progress"]["completed_steps"] == [
        {
            "index": 0,
            "name": "broad-discovery",
            "round_id": "phase_review-round-1",
            "lane": "review_t1",
            "reviewed_head": "head-1",
        }
    ]

    second = orchestrator_runner.run_one_expensive_step(queued, state_dir=tmp_path / "state")

    assert second.ran_step is True
    assert second.state["stage"] == STAGE_DECISION_PENDING
    assert second.state["pending_action"]["step_index"] == 1
    assert second.state["pending_action"]["step"] == "precision-signoff"
    assert second.state["rounds"][1]["round_id"] == "phase_review-round-2"
    assert review_calls[1]["step_name"] == "precision-signoff"

    green = record_clean_decision(second.state, round_id="phase_review-round-2", lane="review_t1", reviewed_head="head-1")

    assert green["stage"] == STAGE_REVIEW_GREEN
    assert green["pending_action"] is None
    assert green["validation"]["review_green"] == "passed"
    assert green["review_progress"]["next_step_index"] == 2
    assert [item["round_id"] for item in green["review_progress"]["completed_steps"]] == [
        "phase_review-round-1",
        "phase_review-round-2",
    ]
    assert len(review_calls) == 2


def test_runner_runs_gate_profile_step_once_after_review_steps(monkeypatch, tmp_path: Path) -> None:
    _stub_review(monkeypatch, "phase_review-round-1")
    gate_calls = _stub_gate(monkeypatch, "phase_gate-round-1")
    monkeypatch.setattr(
        orchestrator_runner,
        "_gate_review_scope_and_prompt",
        lambda **kwargs: ({"base": "main", "reviewed_head": "head-1"}, ""),
    )
    state = _cycle(tmp_path, deslop_enabled=False, step_names=("precision",))
    state["review_plan"]["steps"].append({"name": "local-signoff", "kind": "gate", "gate": "phase_gate"})

    first = orchestrator_runner.run_one_expensive_step(state, state_dir=tmp_path / "state")
    queued = record_clean_decision(first.state, round_id="phase_review-round-1", lane="review_t1", reviewed_head="head-1")
    gate = orchestrator_runner.run_one_expensive_step(queued, state_dir=tmp_path / "state")

    assert gate.ran_step is True
    assert gate.step == "gate"
    assert gate.state["stage"] == STAGE_DECISION_PENDING
    assert gate.state["pending_action"] == {
        "kind": "decision",
        "round_id": "phase_gate-round-1",
        "lane": "review_t2",
        "gate": "phase_gate",
        "step_index": 1,
        "step": "local-signoff",
    }
    assert gate.state["rounds"][1]["kind"] == "gate"
    assert gate.state["rounds"][1]["gate"] == "phase_gate"
    assert gate.state["rounds"][1]["signoff_required"] is True
    assert gate.state["rounds"][1]["output_refs"] == ["rollout://phase_gate-round-1/alpha"]
    assert len(gate_calls) == 1
    assert gate_calls[0]["gate_task_class"] == "phase_gate"
    assert gate_calls[0]["review_scope"]["base"] == "main"

    reprint = orchestrator_runner.run_one_expensive_step(gate.state, state_dir=tmp_path / "state")

    assert reprint.ran_step is False
    assert len(gate_calls) == 1


def test_runner_skips_emergency_deslop(monkeypatch, tmp_path: Path) -> None:
    review_calls = _stub_review(monkeypatch)

    def fail_run(*, command: list[str], cwd: Path) -> subprocess.CompletedProcess:
        raise AssertionError("emergency mode must not run deslop")

    monkeypatch.setattr(orchestrator_runner, "run_deslop_subprocess", fail_run)

    result = orchestrator_runner.run_one_expensive_step(_cycle(tmp_path, mode="emergency", deslop_enabled=False), state_dir=tmp_path / "state")

    assert result.ran_step is True
    assert result.step == "review"
    assert len(review_calls) == 1
    assert result.state["deslop"]["status"] == "skipped-emergency"

    green = record_clean_decision(result.state, round_id="phase_review-round-1", lane="review_t1", reviewed_head="head-1")

    assert green["stage"] == STAGE_REVIEW_GREEN
    assert green["pending_action"] is None


def test_runner_marks_failed_deslop_retryable_and_retries_from_retry_stage(monkeypatch, tmp_path: Path) -> None:
    calls = 0

    def fake_run(*, command: list[str], cwd: Path) -> subprocess.CompletedProcess:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(command, 9 if calls == 1 else 0, stdout="", stderr="")

    monkeypatch.setattr(orchestrator_runner, "run_deslop_subprocess", fake_run)

    failed = orchestrator_runner.run_one_expensive_step(_cycle(tmp_path))

    assert failed.ran_step is True
    assert failed.state["stage"] == STAGE_RETRY_REQUESTED
    assert failed.state["pending_action"] == {"kind": "run-deslop"}
    assert failed.state["deslop"]["status"] == "failed"
    assert failed.state["deslop"]["returncode"] == 9
    assert failed.state["recovery"]["status"] == STAGE_RETRY_REQUESTED
    assert failed.state["recovery"]["retry_count"] == 1

    retried = orchestrator_runner.run_one_expensive_step(failed.state)

    assert retried.ran_step is True
    assert retried.state["stage"] == STAGE_CREATED
    assert retried.state["deslop"]["status"] == "done"
    assert retried.state["recovery"]["status"] == "none"
    assert calls == 2


def test_runner_runs_real_followup_once_from_followup_pending(monkeypatch, tmp_path: Path) -> None:
    followup_calls = _stub_followup(monkeypatch)
    monkeypatch.setattr(orchestrator_runner, "current_head", lambda cwd: "head-2")
    monkeypatch.setattr(orchestrator_runner, "merge_base", lambda cwd, left, right="HEAD": "base-1")
    monkeypatch.setattr(orchestrator_runner, "diff_artifact", lambda cwd, start_ref, end_ref="HEAD": "diff --git a/app.txt b/app.txt\n")
    state = _cycle(tmp_path, deslop_enabled=False, step_names=("broad-discovery", "precision-signoff"))
    pending = mark_review_step_pending(
        state,
        round_id="phase_review-round-1",
        lane="review_t1",
        step_index=0,
        step_name="broad-discovery",
        reviewed_head="head-1",
    )
    findings = record_findings_decision(pending, round_id="phase_review-round-1", lane="review_t1", reviewed_head="head-1")
    fixed = mark_fix_detected(findings, head="head-2")

    result = orchestrator_runner.run_one_expensive_step(fixed, state_dir=tmp_path / "state")

    assert result.ran_step is True
    assert result.step == "review-followup"
    assert result.state["stage"] == STAGE_DECISION_PENDING
    assert result.state["pending_action"] == {
        "kind": "decision",
        "round_id": "followup-round-1",
        "lane": "review-followup",
        "source_round_id": "phase_review-round-1",
    }
    assert result.state["rounds"][1]["lane"] == "review-followup"
    assert result.state["rounds"][1]["kind"] == "followup"
    assert result.state["rounds"][1]["source_round_id"] == "phase_review-round-1"
    assert result.state["rounds"][1]["reviewed_head"] == "head-2"
    assert result.state["rounds"][1]["output_refs"] == ["rollout://followup-round-1/alpha"]
    assert len(followup_calls) == 1
    assert followup_calls[0]["review_scope"]["source_round_id"] == "phase_review-round-1"
    assert followup_calls[0]["review_scope"]["commit"] == "head-1"
    assert followup_calls[0]["review_scope"]["commit_end"] == "head-2"
    assert "Review this follow-up diff" in str(followup_calls[0]["prompt"])
    assert "Source review round phase_review-round-1" in str(followup_calls[0]["prompt"])
