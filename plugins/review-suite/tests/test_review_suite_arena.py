from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from argparse import Namespace

from review_suite_arena import (
    _blocking_round_error,
    _completed_round_payload,
    _has_direct_grade_inputs,
    _normalize_arena_task_class,
    _print_findings,
    _public_local_task_name,
    cmd_close_gate,
    cmd_costs,
    cmd_resume_round,
    cmd_run_manual_round,
    cmd_show_last,
    cmd_show_round,
    run_benchmarked_round,
)
from review_suite_local import public_round_result, write_round


BASIS = "valid_findings_vs_none"


@pytest.fixture(autouse=True)
def _stub_clean_git_worktree(monkeypatch) -> None:
    monkeypatch.setattr("review_suite_arena.ensure_clean_git_worktree", lambda *args, **kwargs: None)


def test_print_findings_prints_final_reviewer_output(capsys) -> None:
    _print_findings(
        {
            "round_id": "round-123",
            "live_completion_statuses": {"Alpha": "completed"},
            "runs": [
                {
                    "slot": "Alpha",
                    "review_status": "completed",
                    "status_summary": "Medium - thing",
                    "grade_blocked": False,
                    "reviewer_output": "full body",
                },
                {
                    "slot": "Bravo",
                    "review_status": "completed",
                    "status_summary": "No findings.",
                    "grade_blocked": False,
                    "reviewer_output": "Bravo body",
                }
            ],
        }
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Output:" in captured.out
    assert "Alpha:" in captured.out
    assert "full body" in captured.out
    assert "Bravo:" in captured.out
    assert "Bravo body" in captured.out


def test_public_round_result_does_not_repeat_streamed_output() -> None:
    result = public_round_result(
        {
            "round_id": "round-123",
            "task_class": "phase_review",
            "status": "completed",
            "runs": [
                {
                    "slot": "alpha",
                    "review_status": "completed",
                    "status_summary": "Medium - thing",
                    "grade_blocked": False,
                    "reviewer_output": "already streamed body",
                    "reviewer_output_ref": "ref://alpha",
                }
            ],
        }
    )

    assert result["runs"][0]["slot"] == "alpha"
    assert "output" not in result["runs"][0]


def test_blocking_round_error_uses_compact_action_for_completed_round() -> None:
    error = _blocking_round_error(
        payload={
            "round_id": "round-123",
            "status": "completed",
            "runs": [{"review_status": "completed", "grade_blocked": False}],
        },
        action="review_t1",
    )

    assert str(error) == "pending round blocks review_t1: round-123"
    assert "grade_command" not in str(error)
    assert "dismiss_command" not in str(error)
    assert "grade --winner WINNER" in str(error.action_payload["cmd"])
    assert "dismiss-round" in str(error.action_payload["dismiss_cmd"])
    assert error.action_payload["note"] == "grade before starting another arena lane"


def test_blocking_round_error_uses_compact_action_for_running_round() -> None:
    error = _blocking_round_error(
        payload={
            "round_id": "round-456",
            "status": "running",
            "runs": [{"review_status": "running"}],
        },
        action="review_t3",
    )

    assert str(error) == "pending round blocks review_t3: round-456 (running)"
    assert "cmd" not in error.action_payload
    assert "dismiss-round" in str(error.action_payload["dismiss_cmd"])
    assert error.action_payload["note"] == "wait for completion before starting another arena lane"


def test_print_findings_uses_recovered_full_body(capsys) -> None:
    _print_findings(
        {
            "round_id": "round-456",
            "live_completion_statuses": {"Alpha": "other_status"},
            "runs": [
                {
                    "slot": "Alpha",
                    "review_status": "completed",
                    "status_summary": "placeholder",
                    "grade_blocked": False,
                    "reviewer_output": "Recovered full body",
                }
            ],
        }
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Alpha:" in captured.out
    assert "placeholder" not in captured.out
    assert "Recovered full body" in captured.out


def test_print_findings_prints_blocked_runs(capsys) -> None:
    _print_findings(
        {
            "runs": [
                {
                    "slot": "Alpha",
                    "review_status": "interrupted_capacity",
                    "status_summary": "capacity",
                    "grade_blocked": True,
                    "grade_block_reason": "selected_model_at_capacity",
                }
            ],
        }
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Alpha [interrupted_capacity]:" in captured.out
    assert "capacity" in captured.out


def test_cmd_show_round_prints_stored_reviewer_outputs(tmp_path: Path, capsys) -> None:
    write_round(
        tmp_path,
        {
            "round_id": "round-1",
            "task_class": "phase_review",
            "status": "completed",
            "runs": [
                {
                    "slot": "alpha",
                    "variant_id": "alpha-model",
                    "review_status": "completed",
                    "reviewer_output": "Alpha finding",
                },
                {
                    "slot": "bravo",
                    "variant_id": "bravo-model",
                    "review_status": "completed",
                    "status_summary": "No findings.",
                },
            ],
        },
    )

    result = cmd_show_round(Namespace(round_id="round-1", state_dir=str(tmp_path), json=False))

    captured = capsys.readouterr()
    assert result == 0
    assert "round_id: round-1" in captured.out
    assert "alpha:" in captured.out
    assert "Alpha finding" in captured.out
    assert "bravo:" in captured.out
    assert "No findings." in captured.out


def test_cmd_show_round_prints_gate_record_outputs(tmp_path: Path, capsys) -> None:
    (tmp_path / "gate_runs.jsonl").write_text(
        json.dumps(
            {
                "recorded_at": "2026-04-25T10:00:00Z",
                "round_id": "gate-round-1",
                "task_class": "pr_gate",
                "review_cwd": str(tmp_path),
                "review_cwd_normalized": str(tmp_path),
                "runs": [
                    {
                        "slot": "alpha",
                        "variant_id": "alpha-model",
                        "review_status": "completed",
                        "reviewer_output": "Gate alpha finding",
                    },
                    {
                        "slot": "bravo",
                        "variant_id": "bravo-model",
                        "review_status": "completed",
                        "status_summary": "Gate bravo clean.",
                    },
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = cmd_show_round(Namespace(round_id="gate-round-1", state_dir=str(tmp_path), json=False))

    captured = capsys.readouterr()
    assert result == 0
    assert "task: review_t4" in captured.out
    assert "Gate alpha finding" in captured.out
    assert "Gate bravo clean." in captured.out


def test_cmd_close_gate_clean_records_workflow_anchor(monkeypatch, tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "gate_runs.jsonl").write_text(
        json.dumps(
            {
                "recorded_at": "2026-04-25T10:00:00Z",
                "round_id": "gate-round-clean",
                "task_class": "pr_gate",
                "task_id": "feature/test",
                "review_cwd": str(repo),
                "review_cwd_normalized": str(repo),
                "review_scope": {"base": "main", "reviewed_head": "head-sha"},
                "signoff_status": "pending",
                "signoff_required": True,
                "runs": [
                    {
                        "slot": "alpha",
                        "variant_id": "alpha-model",
                        "review_status": "completed",
                        "grade_blocked": False,
                        "reviewer_output_ref": "ref://alpha",
                    },
                    {
                        "slot": "bravo",
                        "variant_id": "bravo-model",
                        "review_status": "completed",
                        "grade_blocked": False,
                        "reviewer_output_ref": "ref://bravo",
                    },
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    anchor_calls: list[dict[str, object]] = []
    monkeypatch.setattr("review_suite_arena.record_review_anchor", lambda **kwargs: anchor_calls.append(kwargs) or {})

    result = cmd_close_gate(
        Namespace(round_id="gate-round-clean", verdict="clean", state_dir=str(tmp_path), note=None)
    )

    captured = capsys.readouterr()
    decisions = [json.loads(line) for line in (tmp_path / "gate_signoffs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert result == 0
    assert anchor_calls[0]["lane"] == "review_t4"
    assert anchor_calls[0]["task_id"] == "feature/test"
    assert anchor_calls[0]["output_refs"] == ["ref://alpha", "ref://bravo"]
    assert decisions[0]["verdict"] == "clean"
    assert decisions[0]["workflow_anchor_recorded"] is True
    assert "anchored: true" in captured.out


def test_cmd_close_gate_findings_does_not_record_workflow_anchor(monkeypatch, tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "gate_runs.jsonl").write_text(
        json.dumps(
            {
                "recorded_at": "2026-04-25T10:00:00Z",
                "round_id": "gate-round-findings",
                "task_class": "phase_gate",
                "task_id": "feature/test",
                "review_cwd": str(repo),
                "review_cwd_normalized": str(repo),
                "review_scope": {"base": "main", "reviewed_head": "head-sha"},
                "signoff_status": "pending",
                "signoff_required": True,
                "runs": [
                    {
                        "slot": "alpha",
                        "variant_id": "alpha-model",
                        "review_status": "completed",
                        "grade_blocked": False,
                        "reviewer_output": "P2 finding",
                    },
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    anchor_calls: list[dict[str, object]] = []
    monkeypatch.setattr("review_suite_arena.record_review_anchor", lambda **kwargs: anchor_calls.append(kwargs) or {})

    result = cmd_close_gate(
        Namespace(round_id="gate-round-findings", verdict="findings", state_dir=str(tmp_path), note="valid P2")
    )
    shown = cmd_show_round(Namespace(round_id="gate-round-findings", state_dir=str(tmp_path), json=False))

    captured = capsys.readouterr()
    decisions = [json.loads(line) for line in (tmp_path / "gate_signoffs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert result == 0
    assert shown == 0
    assert anchor_calls == []
    assert decisions[0]["verdict"] == "findings"
    assert decisions[0]["workflow_anchor_recorded"] is False
    assert "status: findings" in captured.out
    assert "signoff: findings" in captured.out
    assert "Code only valid findings" in captured.out
    assert "full-suite/CI continues as a merge-readiness check" in captured.out


def test_cmd_costs_writes_markdown_report(monkeypatch, tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_dir = tmp_path / "state"
    output = tmp_path / "costs.md"
    rows = []

    class Row:
        repo = "repo"
        folder = "repo"
        branch = "feat/costs"
        pr_number = "42"
        worker_model = "gpt-5.5 medium"
        implementation_tokens = 1000
        implementation_cost_usd = 0.004
        caller_threads = ()
        latest_review = "2026-04-27T10:00:00Z"
        lane_sessions = {"review_t1": 2, "review_t2": 2, "review_t3": 0, "review_t4": 0}
        review_seconds = 123.0
        tokens = 456
        cost_usd = 0.012345

    monkeypatch.setattr("review_suite_arena.resolve_repo_root", lambda cd: repo)
    monkeypatch.setattr("review_suite_arena.collect_review_cost_rows", lambda **kwargs: rows or [Row()])

    result = cmd_costs(Namespace(cd=str(repo), all=False, state_dir=str(state_dir), output=str(output), json=False))

    captured = capsys.readouterr()
    assert result == 0
    assert output.exists()
    assert "# repo" in output.read_text(encoding="utf-8")
    assert "rows: 1" in captured.out
    assert "total_tokens: 456" in captured.out
    assert "total_cost_usd: 0.012345" in captured.out
    assert "total_implementation_tokens: 1000" in captured.out
    assert "total_implementation_cost_usd: 0.004" in captured.out


def test_cmd_costs_all_ignores_cd_filter(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_dir = tmp_path / "state"
    seen = {}

    def fake_resolve_repo_root(cd):
        if cd == str(repo):
            raise AssertionError("--all should not resolve --cd")
        return repo

    monkeypatch.setattr("review_suite_arena.resolve_repo_root", fake_resolve_repo_root)
    monkeypatch.setattr("review_suite_arena.write_review_cost_report", lambda **kwargs: tmp_path / "costs.md")
    monkeypatch.setattr("review_suite_arena.emit_toon", lambda payload: None)

    def fake_collect_review_cost_rows(**kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr("review_suite_arena.collect_review_cost_rows", fake_collect_review_cost_rows)

    result = cmd_costs(Namespace(cd=str(repo), all=True, state_dir=str(state_dir), output=None, json=False, codex_home=None))

    assert result == 0
    assert seen["review_cwd"] is None
    assert seen["include_all"] is True


def test_cmd_show_last_prints_latest_outputs_per_local_lane(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    write_round(
        tmp_path,
        {
            "round_id": "older-t1",
            "task_class": "phase_review",
            "status": "completed",
            "sampled_at": "2026-04-25T09:00:00Z",
            "review_cwd": str(repo),
            "review_cwd_normalized": str(repo),
            "runs": [{"slot": "alpha", "review_status": "completed", "reviewer_output": "Old T1"}],
        },
    )
    write_round(
        tmp_path,
        {
            "round_id": "latest-t1",
            "task_class": "phase_review",
            "status": "completed",
            "sampled_at": "2026-04-25T10:00:00Z",
            "review_cwd": str(repo),
            "review_cwd_normalized": str(repo),
            "runs": [{"slot": "alpha", "review_status": "completed", "reviewer_output": "Latest T1"}],
        },
    )
    (tmp_path / "gate_runs.jsonl").write_text(
        json.dumps(
            {
                "recorded_at": "2026-04-25T10:30:00Z",
                "round_id": "latest-t2",
                "task_class": "phase_gate",
                "review_cwd": str(repo),
                "review_cwd_normalized": str(repo),
                "runs": [{"slot": "alpha", "review_status": "completed", "reviewer_output": "Latest T2"}],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = cmd_show_last(Namespace(cd=None, task=None, state_dir=str(tmp_path), json=False))

    captured = capsys.readouterr()
    assert result == 0
    assert "round_id: latest-t1" in captured.out
    assert "Latest T1" in captured.out
    assert "Old T1" not in captured.out
    assert "round_id: latest-t2" in captured.out
    assert "Latest T2" in captured.out


def test_print_findings_prints_blocked_body_after_completion_status(capsys) -> None:
    _print_findings(
        {
            "live_completion_statuses": {"Alpha": "selected_model_at_capacity"},
            "runs": [
                {
                    "slot": "Alpha",
                    "review_status": "interrupted_capacity",
                    "status_summary": "capacity",
                    "grade_blocked": True,
                    "grade_block_reason": "selected_model_at_capacity",
                    "reviewer_output": "Recovered full body",
                }
            ],
        }
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Alpha [interrupted_capacity]:" in captured.out
    assert "Recovered full body" in captured.out


def test_completed_round_payload_uses_reroll_command_key() -> None:
    payload = _completed_round_payload(
        round_result={"blocked": True, "runs": []},
        reroll_rows=[{"slot": "alpha", "command": "reroll-cmd"}],
    )

    assert payload["actions"] == [{"kind": "reroll", "slot": "alpha", "cmd": "reroll-cmd"}]
    assert payload["blocked"] is True


def test_completed_round_payload_success_only_emits_grade_action() -> None:
    payload = _completed_round_payload(
        round_result={
            "blocked": False,
            "runs": [
                {
                    "slot": "alpha",
                    "status": "completed",
                    "summary": "No findings.",
                    "ref": "rollout://secret/model",
                }
            ],
        },
        grade_command="grade-cmd",
    )

    assert payload["Action"]["cmd"] == "grade-cmd"
    assert payload["Action"]["winner"] == ["alpha", "bravo", "tie"]
    assert "valid_findings_vs_none" in payload["Action"]["basis"]
    assert "runs" not in payload


def test_completed_round_payload_omits_inspect_when_round_id_is_known() -> None:
    payload = _completed_round_payload(
        round_result={
            "round_id": "round-123",
            "blocked": False,
            "runs": [],
        },
        grade_command="grade-cmd",
    )

    assert payload["Action"]["cmd"] == "grade-cmd"
    assert "inspect" not in payload["Action"]


def test_completed_round_payload_manual_omits_run_rows_after_output() -> None:
    payload = _completed_round_payload(
        round_result={"round_id": "round-123", "blocked": False, "runs": [{"slot": "alpha"}]},
        manual=True,
    )

    assert payload == {"Action": {"note": "manual review complete"}}


def test_has_direct_grade_inputs_requires_complete_tuple() -> None:
    assert _has_direct_grade_inputs(
        task_id="task-123",
        winner="alpha",
        basis=BASIS,
    )
    assert not _has_direct_grade_inputs(
        task_id="task-123",
        winner="alpha",
        basis=None,
    )


def test_run_benchmarked_round_direct_grade_uses_latest_pending_round(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}
    emitted: list[dict[str, object]] = []

    monkeypatch.setattr("review_suite_arena._ensure_no_pending_grades", lambda **kwargs: (_ for _ in ()).throw(AssertionError("should not check pending before direct grade")))
    monkeypatch.setattr("review_suite_arena.load_roster", lambda path: {"variants": []})
    monkeypatch.setattr("review_suite_arena.load_rubric", lambda path: {"categories": []})
    monkeypatch.setattr(
        "review_suite_arena.find_pending_rounds_for_caller",
        lambda **kwargs: [{"round_id": "latest-round", "task_class": "pr_review"}],
    )

    def fake_record_grade_result(**kwargs):
        captured.update(kwargs)
        return {"status": "graded", "round_id": kwargs["round_id"]}

    monkeypatch.setattr("review_suite_arena._record_grade_result", fake_record_grade_result)
    monkeypatch.setattr("review_suite_arena.emit_toon", lambda payload: emitted.append(payload))

    result = run_benchmarked_round(
        task_class="pr_review",
        review_cwd=tmp_path,
        roster_path=tmp_path / "roster.json",
        rubric_path=tmp_path / "rubric.json",
        state_dir=tmp_path / "state",
        sqlite_path=tmp_path / "state.sqlite",
        seed=None,
        allow_dirty=False,
        progress_interval_seconds=30,
        allow_unsafe_windows_wsl_fallback=False,
        review_scope={"base": "main"},
        prompt="",
        caller_id="caller-1",
        caller_id_source="explicit",
        ignore_pending_grades=False,
        task_id="task-123",
        winner="alpha",
        basis=BASIS,
        note="shared",
        alpha_note="alpha",
        bravo_note="bravo",
    )

    assert result == 0
    assert captured["round_id"] == "latest-round"
    assert captured["task_id"] == "task-123"
    assert captured["winner"] == "alpha"
    assert captured["basis"] == BASIS
    assert emitted == [{"status": "graded", "round_id": "latest-round", "task": "review_t3"}]


def test_run_benchmarked_round_emits_round_banner_and_compact_payload(monkeypatch, tmp_path, capsys) -> None:
    emitted: list[dict[str, object]] = []
    anchor_calls: list[dict[str, object]] = []

    monkeypatch.setattr("review_suite_arena._ensure_no_pending_grades", lambda **kwargs: None)
    monkeypatch.setattr("review_suite_arena.load_roster", lambda path: {"variants": []})
    monkeypatch.setattr("review_suite_arena.load_rubric", lambda path: {"categories": []})
    monkeypatch.setattr("review_suite_arena.read_jsonl", lambda path: [])
    monkeypatch.setattr("review_suite_arena.load_operational_state", lambda path: {})
    monkeypatch.setattr("review_suite_arena.select_pair", lambda **kwargs: {"round_id": "round-1", "task_class": "phase_review"})
    monkeypatch.setattr("review_suite_arena.write_round", lambda *args, **kwargs: None)
    monkeypatch.setattr("review_suite_arena.run_round", lambda **kwargs: {"round_id": "round-1", "task_class": "phase_review", "runs": []})
    monkeypatch.setattr("review_suite_arena.public_round_result", lambda *args, **kwargs: {"blocked": False, "runs": []})
    monkeypatch.setattr("review_suite_arena._print_findings", lambda *args, **kwargs: None)
    monkeypatch.setattr("review_suite_arena._current_branch_name", lambda review_cwd: "branch-1")
    monkeypatch.setattr("review_suite_arena._print_next_steps", lambda **kwargs: None)
    monkeypatch.setattr("review_suite_arena.record_review_anchor", lambda **kwargs: anchor_calls.append(kwargs) or {})
    monkeypatch.setattr("review_suite_arena.emit_toon", lambda payload: emitted.append(payload))

    result = run_benchmarked_round(
        task_class="phase_review",
        review_cwd=tmp_path,
        roster_path=tmp_path / "roster.json",
        rubric_path=tmp_path / "rubric.json",
        state_dir=tmp_path / "state",
        sqlite_path=tmp_path / "state.sqlite",
        seed=None,
        allow_dirty=False,
        progress_interval_seconds=30,
        allow_unsafe_windows_wsl_fallback=False,
        review_scope={"base": "main"},
        prompt="",
        caller_id="caller-1",
        caller_id_source="explicit",
        ignore_pending_grades=False,
        task_id=None,
        winner=None,
        basis=None,
        note=None,
        alpha_note=None,
        bravo_note=None,
        public_task_name="review_t1",
    )

    assert result == 0
    assert emitted[-1]["Action"]["cmd"]
    assert "task" not in emitted[-1]
    assert "round_id" not in emitted[-1]
    assert "runs" not in emitted[-1]
    assert "status" not in emitted[-1]
    assert anchor_calls[0]["lane"] == "review_t1"
    assert anchor_calls[0]["task_id"] == "branch-1"
    assert "[review-suite] round review_t1 round-1" in capsys.readouterr().err


def test_run_benchmarked_round_noninteractive_uses_toon_actions_without_stderr_next_steps(monkeypatch, tmp_path) -> None:
    emitted: list[dict[str, object]] = []

    monkeypatch.setattr("review_suite_arena._ensure_no_pending_grades", lambda **kwargs: None)
    monkeypatch.setattr("review_suite_arena.load_roster", lambda path: {"variants": []})
    monkeypatch.setattr("review_suite_arena.load_rubric", lambda path: {"categories": []})
    monkeypatch.setattr("review_suite_arena.read_jsonl", lambda path: [])
    monkeypatch.setattr("review_suite_arena.load_operational_state", lambda path: {})
    monkeypatch.setattr("review_suite_arena.select_pair", lambda **kwargs: {"round_id": "round-1", "task_class": "phase_review"})
    monkeypatch.setattr("review_suite_arena.write_round", lambda *args, **kwargs: None)
    monkeypatch.setattr("review_suite_arena.run_round", lambda **kwargs: {"round_id": "round-1", "task_class": "phase_review", "runs": []})
    monkeypatch.setattr("review_suite_arena.public_round_result", lambda *args, **kwargs: {"blocked": False, "runs": []})
    monkeypatch.setattr("review_suite_arena._print_findings", lambda *args, **kwargs: None)
    monkeypatch.setattr("review_suite_arena._current_branch_name", lambda review_cwd: "branch-1")
    monkeypatch.setattr("review_suite_arena._output_isatty", lambda: False)
    monkeypatch.setattr(
        "review_suite_arena._print_next_steps",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("next-step prose should stay out of non-interactive output")),
    )
    monkeypatch.setattr("review_suite_arena.emit_toon", lambda payload: emitted.append(payload))

    result = run_benchmarked_round(
        task_class="phase_review",
        review_cwd=tmp_path,
        roster_path=tmp_path / "roster.json",
        rubric_path=tmp_path / "rubric.json",
        state_dir=tmp_path / "state",
        sqlite_path=tmp_path / "state.sqlite",
        seed=None,
        allow_dirty=False,
        progress_interval_seconds=30,
        allow_unsafe_windows_wsl_fallback=False,
        review_scope={"base": "main"},
        prompt="",
        caller_id="caller-1",
        caller_id_source="explicit",
        ignore_pending_grades=False,
        task_id=None,
        winner=None,
        basis=None,
        note=None,
        alpha_note=None,
        bravo_note=None,
        public_task_name="review_t1",
    )

    assert result == 0
    assert set(emitted[-1]) == {"Action"}
    assert "grade --winner WINNER" in emitted[-1]["Action"]["cmd"]
    assert "--round-id" not in emitted[-1]["Action"]["cmd"]
    assert "--task-id" not in emitted[-1]["Action"]["cmd"]
    assert "--basis BASIS" in emitted[-1]["Action"]["cmd"]
    assert "--refresh-report" not in emitted[-1]["Action"]["cmd"]
    assert emitted[-1]["Action"]["winner"] == ["alpha", "bravo", "tie"]
    assert "tie_clean" in emitted[-1]["Action"]["basis"]


def test_run_benchmarked_round_warns_for_deep_review_without_model_names(monkeypatch, tmp_path, capsys) -> None:
    emitted: list[dict[str, object]] = []

    monkeypatch.setattr("review_suite_arena._ensure_no_pending_grades", lambda **kwargs: None)
    monkeypatch.setattr("review_suite_arena.load_roster", lambda path: {"variants": []})
    monkeypatch.setattr("review_suite_arena.load_rubric", lambda path: {"categories": []})
    monkeypatch.setattr("review_suite_arena.read_jsonl", lambda path: [])
    monkeypatch.setattr("review_suite_arena.load_operational_state", lambda path: {})
    monkeypatch.setattr(
        "review_suite_arena.select_pair",
        lambda **kwargs: {
            "round_id": "round-1",
            "task_class": "pr_review",
            "runs": [
                {"slot": "alpha", "variant_id": "hidden-alpha", "reasoning_effort": "xhigh"},
                {"slot": "bravo", "variant_id": "hidden-bravo", "reasoning_effort": "medium"},
            ],
        },
    )
    monkeypatch.setattr("review_suite_arena.write_round", lambda *args, **kwargs: None)
    monkeypatch.setattr("review_suite_arena.run_round", lambda **kwargs: {"round_id": "round-1", "task_class": "pr_review", "runs": []})
    monkeypatch.setattr("review_suite_arena.public_round_result", lambda *args, **kwargs: {"blocked": False, "runs": []})
    monkeypatch.setattr("review_suite_arena._print_findings", lambda *args, **kwargs: None)
    monkeypatch.setattr("review_suite_arena._current_branch_name", lambda review_cwd: "branch-1")
    monkeypatch.setattr("review_suite_arena._output_isatty", lambda: False)
    monkeypatch.setattr("review_suite_arena.record_review_anchor", lambda **kwargs: {})
    monkeypatch.setattr("review_suite_arena.emit_toon", lambda payload: emitted.append(payload))

    result = run_benchmarked_round(
        task_class="pr_review",
        review_cwd=tmp_path,
        roster_path=tmp_path / "roster.json",
        rubric_path=tmp_path / "rubric.json",
        state_dir=tmp_path / "state",
        sqlite_path=tmp_path / "state.sqlite",
        seed=None,
        allow_dirty=False,
        progress_interval_seconds=30,
        allow_unsafe_windows_wsl_fallback=False,
        review_scope={"base": "main"},
        prompt="",
        caller_id="caller-1",
        caller_id_source="explicit",
        ignore_pending_grades=False,
        task_id=None,
        winner=None,
        basis=None,
        note=None,
        alpha_note=None,
        bravo_note=None,
        public_task_name="review_t3",
    )

    assert result == 0
    err = capsys.readouterr().err
    assert "reviews can take up to 20m" in err
    assert "hidden-alpha" not in err
    assert "hidden-bravo" not in err
    assert "xhigh" not in err


def test_run_benchmarked_round_dirty_base_guard_does_not_persist_sampled_round(monkeypatch, tmp_path) -> None:
    writes: list[dict[str, object]] = []

    monkeypatch.setattr("review_suite_arena._ensure_no_pending_grades", lambda **kwargs: None)

    def fail_clean_check(*args, **kwargs):
        raise ValueError("review-suite requires a clean worktree")

    monkeypatch.setattr("review_suite_arena.ensure_clean_git_worktree", fail_clean_check)
    monkeypatch.setattr("review_suite_arena.select_pair", lambda **kwargs: (_ for _ in ()).throw(AssertionError("select_pair should not run")))
    monkeypatch.setattr("review_suite_arena.write_round", lambda state_dir, payload: writes.append(dict(payload)))

    with pytest.raises(ValueError, match="clean worktree"):
        run_benchmarked_round(
            task_class="phase_review",
            review_cwd=tmp_path,
            roster_path=tmp_path / "roster.json",
            rubric_path=tmp_path / "rubric.json",
            state_dir=tmp_path / "state",
            sqlite_path=tmp_path / "state.sqlite",
            seed=None,
            allow_dirty=False,
            progress_interval_seconds=30,
            allow_unsafe_windows_wsl_fallback=False,
            review_scope={"base": "main"},
            prompt="",
            caller_id="caller-1",
            caller_id_source="explicit",
            ignore_pending_grades=False,
            task_id=None,
            winner=None,
            basis=None,
            note=None,
            alpha_note=None,
            bravo_note=None,
            public_task_name="review_t1",
        )

    assert writes == []


def test_run_benchmarked_round_runtime_guard_does_not_persist_sampled_round(monkeypatch, tmp_path) -> None:
    writes: list[dict[str, object]] = []

    monkeypatch.setattr("review_suite_arena._ensure_no_pending_grades", lambda **kwargs: None)
    monkeypatch.setattr("review_suite_arena.ensure_clean_git_worktree", lambda *args, **kwargs: None)

    def fail_runtime_check(**kwargs):
        raise ValueError("rerun with --wsl")

    monkeypatch.setattr("review_suite_arena.validate_codex_runtime", fail_runtime_check)
    monkeypatch.setattr("review_suite_arena.select_pair", lambda **kwargs: (_ for _ in ()).throw(AssertionError("select_pair should not run")))
    monkeypatch.setattr("review_suite_arena.write_round", lambda state_dir, payload: writes.append(dict(payload)))

    with pytest.raises(ValueError, match="--wsl"):
        run_benchmarked_round(
            task_class="phase_review",
            review_cwd=tmp_path,
            roster_path=tmp_path / "roster.json",
            rubric_path=tmp_path / "rubric.json",
            state_dir=tmp_path / "state",
            sqlite_path=tmp_path / "state.sqlite",
            seed=None,
            allow_dirty=False,
            progress_interval_seconds=30,
            allow_unsafe_windows_wsl_fallback=False,
            review_scope={"base": "main"},
            prompt="",
            caller_id="caller-1",
            caller_id_source="explicit",
            ignore_pending_grades=False,
            task_id=None,
            winner=None,
            basis=None,
            note=None,
            alpha_note=None,
            bravo_note=None,
            public_task_name="review_t1",
        )

    assert writes == []


def test_run_benchmarked_round_interactive_blocked_round_skips_final_toon(monkeypatch, tmp_path) -> None:
    next_steps: list[dict[str, object]] = []

    monkeypatch.setattr("review_suite_arena._ensure_no_pending_grades", lambda **kwargs: None)
    monkeypatch.setattr("review_suite_arena.load_roster", lambda path: {"variants": []})
    monkeypatch.setattr("review_suite_arena.load_rubric", lambda path: {"categories": []})
    monkeypatch.setattr("review_suite_arena.read_jsonl", lambda path: [])
    monkeypatch.setattr("review_suite_arena.load_operational_state", lambda path: {})
    monkeypatch.setattr("review_suite_arena.select_pair", lambda **kwargs: {"round_id": "round-1", "task_class": "phase_review"})
    monkeypatch.setattr("review_suite_arena.write_round", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "review_suite_arena.run_round",
        lambda **kwargs: {
            "round_id": "round-1",
            "task_class": "phase_review",
            "runs": [{"slot": "alpha", "review_status": "timeout", "grade_blocked": True}],
        },
    )
    monkeypatch.setattr(
        "review_suite_arena.public_round_result",
        lambda *args, **kwargs: {"blocked": True, "runs": [{"slot": "alpha", "blocked": True}]},
    )
    monkeypatch.setattr("review_suite_arena._print_findings", lambda *args, **kwargs: None)
    monkeypatch.setattr("review_suite_arena._current_branch_name", lambda review_cwd: "branch-1")
    monkeypatch.setattr("review_suite_arena._output_isatty", lambda: True)
    monkeypatch.setattr("review_suite_arena._print_round_banner", lambda **kwargs: None)
    monkeypatch.setattr("review_suite_arena._print_next_steps", lambda **kwargs: next_steps.append(kwargs))
    monkeypatch.setattr(
        "review_suite_arena.emit_toon",
        lambda payload: (_ for _ in ()).throw(AssertionError("interactive blocked runs should not emit final TOON")),
    )

    result = run_benchmarked_round(
        task_class="phase_review",
        review_cwd=tmp_path,
        roster_path=tmp_path / "roster.json",
        rubric_path=tmp_path / "rubric.json",
        state_dir=tmp_path / "state",
        sqlite_path=tmp_path / "state.sqlite",
        seed=None,
        allow_dirty=False,
        progress_interval_seconds=30,
        allow_unsafe_windows_wsl_fallback=False,
        review_scope={"base": "main"},
        prompt="",
        caller_id="caller-1",
        caller_id_source="explicit",
        ignore_pending_grades=False,
        task_id=None,
        winner=None,
        basis=None,
        note=None,
        alpha_note=None,
        bravo_note=None,
        public_task_name="review_t1",
    )

    assert result == 0
    assert next_steps


def test_run_benchmarked_round_direct_grade_rejects_ambiguous_caller_pending_rounds(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "review_suite_arena.find_pending_rounds_for_caller",
        lambda **kwargs: [
            {"round_id": "round-a", "task_class": "pr_review"},
            {"round_id": "round-b", "task_class": "pr_review"},
        ],
    )

    with pytest.raises(ValueError, match="multiple pending pr_review rounds found for this caller"):
        run_benchmarked_round(
            task_class="pr_review",
            review_cwd=tmp_path,
            roster_path=tmp_path / "roster.json",
            rubric_path=tmp_path / "rubric.json",
            state_dir=tmp_path / "state",
            sqlite_path=tmp_path / "state.sqlite",
            seed=None,
            allow_dirty=False,
            progress_interval_seconds=30,
            allow_unsafe_windows_wsl_fallback=False,
            review_scope={"base": "main"},
            prompt="",
            caller_id="caller-1",
            caller_id_source="explicit",
            ignore_pending_grades=False,
            task_id="task-123",
            winner="alpha",
            basis=BASIS,
            note=None,
            alpha_note=None,
            bravo_note=None,
        )


def test_run_benchmarked_round_complete_grade_inputs_without_pending_falls_through(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("review_suite_arena._ensure_no_pending_grades", lambda **kwargs: None)
    monkeypatch.setattr("review_suite_arena.find_pending_rounds_for_caller", lambda **kwargs: [])
    monkeypatch.setattr("review_suite_arena.iter_round_payloads", lambda state_dir: [])
    monkeypatch.setattr("review_suite_arena.load_roster", lambda path: {"variants": []})
    monkeypatch.setattr("review_suite_arena.load_rubric", lambda path: {"categories": []})
    monkeypatch.setattr("review_suite_arena.read_jsonl", lambda path: [])
    monkeypatch.setattr("review_suite_arena.load_operational_state", lambda path: {})
    monkeypatch.setattr("review_suite_arena.select_pair", lambda **kwargs: {"round_id": "new-round", "task_class": "pr_review"})
    monkeypatch.setattr("review_suite_arena.write_round", lambda *args, **kwargs: None)
    monkeypatch.setattr("review_suite_arena.run_round", lambda **kwargs: {"round_id": "new-round", "task_class": "pr_review", "runs": []})
    monkeypatch.setattr("review_suite_arena.public_round_result", lambda *args, **kwargs: {"blocked": False, "runs": []})
    monkeypatch.setattr("review_suite_arena._print_findings", lambda *args, **kwargs: None)
    monkeypatch.setattr("review_suite_arena._current_branch_name", lambda review_cwd: "branch-1")
    monkeypatch.setattr("review_suite_arena._print_next_steps", lambda **kwargs: None)
    monkeypatch.setattr("review_suite_arena._record_grade_result", lambda **kwargs: {"status": "graded", "round_id": kwargs["round_id"]})
    monkeypatch.setattr("review_suite_arena.emit_toon", lambda payload: None)

    result = run_benchmarked_round(
        task_class="pr_review",
        review_cwd=tmp_path,
        roster_path=tmp_path / "roster.json",
        rubric_path=tmp_path / "rubric.json",
        state_dir=tmp_path / "state",
        sqlite_path=tmp_path / "state.sqlite",
        seed=None,
        allow_dirty=False,
        progress_interval_seconds=30,
        allow_unsafe_windows_wsl_fallback=False,
        review_scope={"base": "main"},
        prompt="",
        caller_id="caller-1",
        caller_id_source="explicit",
        ignore_pending_grades=False,
        task_id="task-123",
        winner="alpha",
        basis=BASIS,
        note=None,
        alpha_note=None,
        bravo_note=None,
    )

    assert result == 0


def test_run_benchmarked_round_direct_grade_without_caller_uses_single_repo_pending_round(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}
    emitted: list[dict[str, object]] = []

    monkeypatch.setattr("review_suite_arena._ensure_no_pending_grades", lambda **kwargs: (_ for _ in ()).throw(AssertionError("should not check pending before direct grade")))
    monkeypatch.setattr("review_suite_arena.load_roster", lambda path: {"variants": []})
    monkeypatch.setattr("review_suite_arena.load_rubric", lambda path: {"categories": []})
    monkeypatch.setattr("review_suite_arena.find_pending_rounds_for_caller", lambda **kwargs: [])
    monkeypatch.setattr(
        "review_suite_arena.iter_round_payloads",
        lambda state_dir: [
            {
                "round_id": "repo-round",
                "task_class": "pr_review",
                "status": "completed",
                "review_cwd_normalized": str(tmp_path),
                "runs": [
                    {"slot": "alpha", "review_status": "completed", "grade_blocked": False},
                    {"slot": "bravo", "review_status": "completed", "grade_blocked": False},
                ],
            }
        ],
    )

    def fake_record_grade_result(**kwargs):
        captured.update(kwargs)
        return {"status": "graded", "round_id": kwargs["round_id"]}

    monkeypatch.setattr("review_suite_arena._record_grade_result", fake_record_grade_result)
    monkeypatch.setattr("review_suite_arena.emit_toon", lambda payload: emitted.append(payload))

    result = run_benchmarked_round(
        task_class="pr_review",
        review_cwd=tmp_path,
        roster_path=tmp_path / "roster.json",
        rubric_path=tmp_path / "rubric.json",
        state_dir=tmp_path / "state",
        sqlite_path=tmp_path / "state.sqlite",
        seed=None,
        allow_dirty=False,
        progress_interval_seconds=30,
        allow_unsafe_windows_wsl_fallback=False,
        review_scope={"base": "main"},
        prompt="",
        caller_id=None,
        caller_id_source=None,
        ignore_pending_grades=False,
        task_id="task-123",
        winner="alpha",
        basis=BASIS,
        note=None,
        alpha_note=None,
        bravo_note=None,
    )

    assert result == 0
    assert captured["round_id"] == "repo-round"
    assert emitted == [{"status": "graded", "round_id": "repo-round", "task": "review_t3"}]


def test_run_benchmarked_round_direct_grade_with_caller_falls_back_to_unique_repo_pending_round(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}
    emitted: list[dict[str, object]] = []

    monkeypatch.setattr("review_suite_arena._ensure_no_pending_grades", lambda **kwargs: (_ for _ in ()).throw(AssertionError("should not check pending before direct grade")))
    monkeypatch.setattr("review_suite_arena.load_roster", lambda path: {"variants": []})
    monkeypatch.setattr("review_suite_arena.load_rubric", lambda path: {"categories": []})
    monkeypatch.setattr("review_suite_arena.find_pending_rounds_for_caller", lambda **kwargs: [])
    monkeypatch.setattr(
        "review_suite_arena.iter_round_payloads",
        lambda state_dir: [
            {
                "round_id": "repo-round",
                "task_class": "pr_review",
                "status": "completed",
                "review_cwd_normalized": str(tmp_path),
                "runs": [
                    {"slot": "alpha", "review_status": "completed", "grade_blocked": False},
                    {"slot": "bravo", "review_status": "completed", "grade_blocked": False},
                ],
            }
        ],
    )

    def fake_record_grade_result(**kwargs):
        captured.update(kwargs)
        return {"status": "graded", "round_id": kwargs["round_id"]}

    monkeypatch.setattr("review_suite_arena._record_grade_result", fake_record_grade_result)
    monkeypatch.setattr("review_suite_arena.emit_toon", lambda payload: emitted.append(payload))

    result = run_benchmarked_round(
        task_class="pr_review",
        review_cwd=tmp_path,
        roster_path=tmp_path / "roster.json",
        rubric_path=tmp_path / "rubric.json",
        state_dir=tmp_path / "state",
        sqlite_path=tmp_path / "state.sqlite",
        seed=None,
        allow_dirty=False,
        progress_interval_seconds=30,
        allow_unsafe_windows_wsl_fallback=False,
        review_scope={"base": "main"},
        prompt="",
        caller_id="caller-1",
        caller_id_source="explicit",
        ignore_pending_grades=False,
        task_id="task-123",
        winner="alpha",
        basis=BASIS,
        note=None,
        alpha_note=None,
        bravo_note=None,
    )

    assert result == 0
    assert captured["round_id"] == "repo-round"
    assert emitted == [{"status": "graded", "round_id": "repo-round", "task": "review_t3"}]


def test_run_benchmarked_round_direct_grade_without_caller_rejects_ambiguous_repo_pending_rounds(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("review_suite_arena.find_pending_rounds_for_caller", lambda **kwargs: [])
    monkeypatch.setattr(
        "review_suite_arena.iter_round_payloads",
        lambda state_dir: [
            {"round_id": "round-a", "task_class": "pr_review", "status": "completed", "review_cwd_normalized": str(tmp_path), "runs": []},
            {"round_id": "round-b", "task_class": "pr_review", "status": "completed", "review_cwd_normalized": str(tmp_path), "runs": []},
        ],
    )

    with pytest.raises(ValueError, match="multiple pending pr_review rounds found"):
        run_benchmarked_round(
            task_class="pr_review",
            review_cwd=tmp_path,
            roster_path=tmp_path / "roster.json",
            rubric_path=tmp_path / "rubric.json",
            state_dir=tmp_path / "state",
            sqlite_path=tmp_path / "state.sqlite",
            seed=None,
            allow_dirty=False,
            progress_interval_seconds=30,
            allow_unsafe_windows_wsl_fallback=False,
            review_scope={"base": "main"},
            prompt="",
            caller_id=None,
            caller_id_source=None,
            ignore_pending_grades=False,
            task_id="task-123",
            winner="alpha",
            basis=BASIS,
            note=None,
            alpha_note=None,
            bravo_note=None,
        )


def test_cmd_run_manual_round_emits_manual_payload(monkeypatch, tmp_path, capsys) -> None:
    diff_path = tmp_path / "diff.txt"
    diff_path.write_text("diff --git a/x b/x\n", encoding="utf-8")
    captured: dict[str, object] = {}

    monkeypatch.setattr("review_suite_arena._resolve_review_cwd", lambda cd: tmp_path)
    monkeypatch.setattr("review_suite_arena.load_roster", lambda path: {"variants": []})
    monkeypatch.setattr(
        "review_suite_arena.load_round",
        lambda state_dir, round_id: {"round_id": "round-1", "task_class": "phase_review", "review_cwd": str(tmp_path)},
    )
    monkeypatch.setattr("review_suite_arena._load_manual_instructions", lambda args: "Review this.")
    monkeypatch.setattr("review_suite_arena.build_manual_review_prompt", lambda **kwargs: "prompt")
    monkeypatch.setattr(
        "review_suite_arena.run_round",
        lambda **kwargs: {
            "round_id": "round-1",
            "task_class": "phase_review",
            "runs": [{"slot": "alpha", "review_status": "completed", "reviewer_output": "Manual body"}],
        },
    )
    monkeypatch.setattr("review_suite_arena._visible_completed_output_slots", lambda **kwargs: set())
    monkeypatch.setattr("review_suite_arena.public_round_result", lambda *args, **kwargs: {"blocked": False, "runs": []})
    monkeypatch.setattr("review_suite_arena._completed_round_payload", lambda **kwargs: captured.update(kwargs) or {"status": "ok"})
    monkeypatch.setattr("review_suite_arena.emit_toon", lambda payload: None)

    result = cmd_run_manual_round(
        Namespace(
            cd=None,
            round_id="round-1",
            roster=str(tmp_path / "roster.json"),
            state_dir=str(tmp_path / "state"),
            sqlite_path=str(tmp_path / "state.sqlite"),
            diff_file=str(diff_path),
            instructions="Review this.",
            instructions_file=None,
            progress_interval_seconds=30,
            wsl=False,
        )
    )

    assert result == 0
    assert captured["manual"] is True
    assert "task_name" not in captured
    assert "round_id" not in captured
    assert "Manual body" in capsys.readouterr().out


def test_cmd_reroll_slot_records_workflow_anchor_when_completed(monkeypatch, tmp_path) -> None:
    anchor_calls: list[dict[str, object]] = []
    emitted: list[dict[str, object]] = []

    monkeypatch.setattr("review_suite_arena.load_roster", lambda path: {"variants": []})
    monkeypatch.setattr("review_suite_arena.read_jsonl", lambda path: [])
    monkeypatch.setattr(
        "review_suite_arena.load_round",
        lambda state_dir, round_id: {
            "round_id": "round-1",
            "task_class": "pr_review",
            "review_cwd": str(tmp_path),
            "review_scope": {"base": "main"},
            "requested_prompt": "",
        },
    )
    monkeypatch.setattr(
        "review_suite_arena.build_reroll_slot_payload",
        lambda **kwargs: {"round_id": "round-2", "task_class": "pr_review", "runs": []},
    )
    monkeypatch.setattr("review_suite_arena.run_round", lambda **kwargs: {"round_id": "round-2", "task_class": "pr_review", "runs": []})
    monkeypatch.setattr("review_suite_arena.public_round_result", lambda *args, **kwargs: {"blocked": False, "runs": []})
    monkeypatch.setattr("review_suite_arena._print_findings", lambda *args, **kwargs: None)
    monkeypatch.setattr("review_suite_arena._current_branch_name", lambda review_cwd: "branch-1")
    monkeypatch.setattr("review_suite_arena._output_isatty", lambda: False)
    monkeypatch.setattr("review_suite_arena.write_round", lambda *args, **kwargs: None)
    monkeypatch.setattr("review_suite_arena.record_review_anchor", lambda **kwargs: anchor_calls.append(kwargs) or {})
    monkeypatch.setattr("review_suite_arena.emit_toon", lambda payload: emitted.append(payload))

    result = __import__("review_suite_arena").cmd_reroll_slot(
        Namespace(
            round_id="round-1",
            slot="alpha",
            cd=str(tmp_path),
            base="main",
            seed=None,
            roster=str(tmp_path / "roster.json"),
            rubric=str(tmp_path / "rubric.json"),
            state_dir=str(tmp_path / "state"),
            sqlite_path=str(tmp_path / "state.sqlite"),
            allow_dirty=True,
            progress_interval_seconds=30,
            wsl=False,
        )
    )

    assert result == 0
    assert emitted[0]["Action"]["cmd"]
    assert "runs" not in emitted[0]
    assert anchor_calls[0]["lane"] == "review_t3"
    assert anchor_calls[0]["task_id"] == "branch-1"


def test_cmd_run_round_records_workflow_anchor_when_completed(monkeypatch, tmp_path, capsys) -> None:
    anchor_calls: list[dict[str, object]] = []
    emitted: list[dict[str, object]] = []

    monkeypatch.setattr("review_suite_arena.load_roster", lambda path: {"variants": []})
    monkeypatch.setattr(
        "review_suite_arena.load_round",
        lambda state_dir, round_id: {
            "round_id": "round-1",
            "task_class": "pr_review",
            "review_cwd": str(tmp_path),
            "review_scope": {"base": "main"},
            "requested_prompt": "",
        },
    )
    monkeypatch.setattr(
        "review_suite_arena.run_round",
        lambda **kwargs: {
            "round_id": "round-1",
            "task_class": "pr_review",
            "review_scope": {"base": "main"},
            "runs": [{"slot": "alpha", "review_status": "completed", "reviewer_output": "Run-round body"}],
        },
    )
    monkeypatch.setattr("review_suite_arena.write_round", lambda *args, **kwargs: None)
    monkeypatch.setattr("review_suite_arena.public_round_result", lambda *args, **kwargs: {"blocked": False, "runs": []})
    monkeypatch.setattr("review_suite_arena._resolve_review_cwd", lambda cd: tmp_path)
    monkeypatch.setattr("review_suite_arena._current_branch_name", lambda review_cwd: "branch-1")
    monkeypatch.setattr("review_suite_arena._output_isatty", lambda: False)
    monkeypatch.setattr("review_suite_arena.record_review_anchor", lambda **kwargs: anchor_calls.append(kwargs) or {})
    monkeypatch.setattr("review_suite_arena.emit_toon", lambda payload: emitted.append(payload))

    result = __import__("review_suite_arena").cmd_run_round(
        Namespace(
            round_id="round-1",
            cd=str(tmp_path),
            base="main",
            roster=str(tmp_path / "roster.json"),
            state_dir=str(tmp_path / "state"),
            sqlite_path=str(tmp_path / "state.sqlite"),
            progress_interval_seconds=30,
            allow_dirty=True,
            wsl=False,
        )
    )

    assert result == 0
    assert emitted[0]["Action"]["cmd"]
    assert "runs" not in emitted[0]
    assert anchor_calls[0]["lane"] == "review_t3"
    assert anchor_calls[0]["task_id"] == "branch-1"
    assert "Run-round body" in capsys.readouterr().out


def test_cmd_resume_round_records_workflow_anchor_when_completed(monkeypatch, tmp_path, capsys) -> None:
    anchor_calls: list[dict[str, object]] = []
    emitted: list[dict[str, object]] = []

    monkeypatch.setattr("review_suite_arena.load_roster", lambda path: {"variants": []})
    monkeypatch.setattr(
        "review_suite_arena.load_round",
        lambda state_dir, round_id: {
            "round_id": "round-1",
            "task_class": "pr_review",
            "status": "running",
            "review_cwd": str(tmp_path),
            "review_scope": {"base": "main"},
        },
    )
    monkeypatch.setattr(
        "review_suite_arena.collect_round_results",
        lambda **kwargs: {
            "round_id": "round-1",
            "task_class": "pr_review",
            "review_scope": {"base": "main"},
            "runs": [{"slot": "alpha", "review_status": "completed", "reviewer_output": "Resume body"}],
        },
    )
    monkeypatch.setattr("review_suite_arena.public_round_result", lambda *args, **kwargs: {"blocked": False, "runs": []})
    monkeypatch.setattr("review_suite_arena._current_branch_name", lambda review_cwd: "branch-1")
    monkeypatch.setattr("review_suite_arena._output_isatty", lambda: False)
    monkeypatch.setattr("review_suite_arena.record_review_anchor", lambda **kwargs: anchor_calls.append(kwargs) or {})
    monkeypatch.setattr("review_suite_arena.emit_toon", lambda payload: emitted.append(payload))

    result = cmd_resume_round(
        Namespace(
            round_id="round-1",
            cd=None,
            roster=str(tmp_path / "roster.json"),
            state_dir=str(tmp_path / "state"),
            sqlite_path=str(tmp_path / "state.sqlite"),
            progress_interval_seconds=30,
        )
    )

    assert result == 0
    assert emitted[0]["Action"]["cmd"]
    assert "runs" not in emitted[0]
    assert anchor_calls[0]["lane"] == "review_t3"
    assert anchor_calls[0]["task_id"] == "branch-1"
    assert "Resume body" in capsys.readouterr().out


def test_public_local_task_name_maps_gate_aliases() -> None:
    assert _public_local_task_name("phase_gate") == "review_t2"
    assert _public_local_task_name("pr_gate") == "review_t4"


def test_normalize_arena_task_class_accepts_public_aliases() -> None:
    assert _normalize_arena_task_class("review_t1") == "phase_review"
    assert _normalize_arena_task_class("review_t3") == "pr_review"


def test_cmd_sample_emits_public_task_alias(monkeypatch, tmp_path) -> None:
    emitted: list[dict[str, object]] = []
    captured: dict[str, object] = {}
    repo_root = tmp_path / "repo-root"

    monkeypatch.setattr("review_suite_arena.resolve_caller_id", lambda caller_id: (None, None))
    monkeypatch.setattr("review_suite_arena._resolve_review_cwd", lambda cd: repo_root)
    monkeypatch.setattr("review_suite_arena.load_roster", lambda path: {"variants": []})
    monkeypatch.setattr("review_suite_arena.read_jsonl", lambda path: [])
    monkeypatch.setattr("review_suite_arena.load_operational_state", lambda path: {"task_classes": {}})
    monkeypatch.setattr(
        "review_suite_arena.select_pair",
        lambda **kwargs: captured.update(kwargs) or {"round_id": "round-1", "task_class": "phase_review", "status": "sampled", "runs": []},
    )
    monkeypatch.setattr("review_suite_arena.write_round", lambda *args, **kwargs: None)
    monkeypatch.setattr("review_suite_arena.emit_toon", lambda payload: emitted.append(payload))

    result = __import__("review_suite_arena").cmd_sample(
        Namespace(
            task_class="review_t1",
            caller_id=None,
            roster=str(tmp_path / "roster.json"),
            state_dir=str(tmp_path / "state"),
            ignore_pending_grades=True,
            seed=None,
            exclude_variant_id=[],
        )
    )

    assert result == 0
    assert captured["task_class"] == "phase_review"
    assert captured["review_cwd"] == repo_root
    assert emitted == [{"round_id": "round-1", "task": "review_t1", "status": "sampled", "sampled_at": None, "reviewers": []}]
