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
    record_clean_decision,
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
