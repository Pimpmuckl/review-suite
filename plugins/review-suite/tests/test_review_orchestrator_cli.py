from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import review
import review_suite_arena
from review_suite_core import orchestrator_runner


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout or f"git {' '.join(args)} failed")
    return proc.stdout.strip()


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "checkout", "-b", "main")
    _git(repo, "config", "user.email", "codex@example.invalid")
    _git(repo, "config", "user.name", "Codex")


def _commit_file(repo: Path, relative_path: str, content: str, message: str) -> str:
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(repo, "add", relative_path)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _run_review(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> tuple[int, dict[str, object]]:
    emitted: list[dict[str, object]] = []
    monkeypatch.setattr(review, "emit_toon", lambda payload: emitted.append(payload))
    monkeypatch.setattr(sys, "argv", ["review.py", *args])

    exit_code = review.main()

    assert len(emitted) == 1
    return exit_code, emitted[0]


def _stub_deslop(monkeypatch: pytest.MonkeyPatch, *returncodes: int) -> list[list[str]]:
    calls: list[list[str]] = []
    codes = list(returncodes) or [0]

    def fake_run(*, command: list[str], cwd: Path) -> subprocess.CompletedProcess:
        calls.append(command)
        index = min(len(calls) - 1, len(codes) - 1)
        return subprocess.CompletedProcess(command, codes[index], stdout="", stderr="")

    monkeypatch.setattr(orchestrator_runner, "run_deslop_subprocess", fake_run)
    return calls


def _stub_review(monkeypatch: pytest.MonkeyPatch, *round_ids: str) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    ids = list(round_ids) or ["phase_review-round-1"]

    def fake_run(**kwargs: object) -> dict[str, object]:
        calls.append(dict(kwargs))
        round_id = ids[min(len(calls) - 1, len(ids) - 1)]
        scope = kwargs.get("review_scope")
        reviewed_head = str(scope.get("reviewed_head") if isinstance(scope, dict) else "head-1")
        return {
            "round_id": round_id,
            "lane": "review_t1",
            "kind": "review",
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

    monkeypatch.setattr(orchestrator_runner, "run_review_step", fake_run)
    return calls


def _stub_followup(monkeypatch: pytest.MonkeyPatch, *round_ids: str) -> list[dict[str, object]]:
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


def _stub_gate(monkeypatch: pytest.MonkeyPatch, *round_ids: str) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    ids = list(round_ids) or ["phase_gate-round-1"]

    def fake_run(**kwargs: object) -> tuple[dict[str, object], int]:
        calls.append(dict(kwargs))
        round_id = ids[min(len(calls) - 1, len(ids) - 1)]
        state_dir = Path(kwargs["state_dir"])
        review_cwd = Path(kwargs["review_cwd"])
        review_scope = dict(kwargs.get("review_scope") or {})
        ref = f"rollout://{round_id}/alpha"
        record = {
            "round_id": round_id,
            "task_class": kwargs["gate_task_class"],
            "task_id": kwargs.get("task_id") or round_id,
            "review_cwd": str(review_cwd),
            "review_cwd_normalized": str(review_cwd),
            "review_scope": review_scope,
            "signoff_status": "pending",
            "signoff_required": True,
            "runs": [{"slot": "alpha", "reviewer_output_ref": ref, "grade_blocked": False}],
        }
        path = state_dir / "gate_runs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
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
                        "ref": ref,
                    }
                ],
            },
            0,
        )

    monkeypatch.setattr(orchestrator_runner, "run_gate_step", fake_run)
    return calls


def _cycle_payload(state_dir: Path, public_id: str) -> dict[str, object]:
    index = json.loads((state_dir / "orchestrator" / "index.json").read_text(encoding="utf-8"))
    cycle_key = index["ids"][public_id]
    return json.loads((state_dir / "orchestrator" / "cycles" / f"{cycle_key}.json").read_text(encoding="utf-8"))


def _gate_signoff_decisions(state_dir: Path) -> list[dict[str, object]]:
    path = state_dir / "gate_signoffs.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _assert_github_handoff(action: object, *, blocked_by: list[str]) -> None:
    assert isinstance(action, dict)
    payload = dict(action)
    assert payload["kind"] == "github-handoff"
    assert payload["lane"] == "review-github"
    assert payload["after"] == "PR create/update"
    assert payload["github_review"] == "not-run"
    assert "merge_ready" not in payload
    assert "cmd" not in payload
    if blocked_by:
        assert payload["validation_ready"] is False
        assert payload["blocked_by"] == blocked_by
    else:
        assert payload["validation_ready"] is True
        assert "blocked_by" not in payload


def test_orchestrator_review_helper_uses_phase_prompt_without_predecision_anchor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")
    _git(repo, "checkout", "-b", "feature/orchestrator-review")
    head = _commit_file(repo, "app.txt", "feature\n", "feature")
    captured: dict[str, object] = {}
    anchor_calls: list[dict[str, object]] = []

    def fake_run_round(
        *,
        round_payload: dict[str, object],
        roster: dict[str, object],
        state_dir: Path,
        review_cwd: Path,
        prompt: str,
        review_scope: dict[str, object],
        sqlite_path: Path,
        progress_interval_seconds: int,
        allow_dirty: bool,
        allow_unsafe_windows_wsl_fallback: bool,
    ) -> dict[str, object]:
        captured["prompt"] = prompt
        captured["review_scope"] = dict(review_scope)
        return {
            **round_payload,
            "status": "completed",
            "review_scope": dict(review_scope),
            "runs": [
                {
                    "slot": "alpha",
                    "review_status": "completed",
                    "status_summary": "No findings.",
                    "grade_blocked": False,
                    "grade_block_reason": None,
                    "reviewer_output": "No findings.",
                    "reviewer_output_ref": "rollout://alpha",
                }
            ],
        }

    monkeypatch.setattr(review_suite_arena, "run_round", fake_run_round)
    monkeypatch.setattr(review_suite_arena, "_print_round_banner", lambda **kwargs: None)
    monkeypatch.setattr(review_suite_arena, "_print_findings", lambda result: False)
    monkeypatch.setattr(review_suite_arena, "record_review_anchor", lambda **kwargs: anchor_calls.append(kwargs))

    result = review_suite_arena.run_orchestrator_review_step(
        lane="review_t1",
        step_name="precision",
        reviewer_count=1,
        model="gpt-5.5",
        reasoning_effort="medium",
        service_tier=None,
        review_cwd=repo,
        state_dir=state_dir,
        sqlite_path=tmp_path / "state.sqlite",
        review_scope={"base": "main"},
        task_id="feature/orchestrator-review",
        allow_dirty=False,
        progress_interval_seconds=1,
        allow_unsafe_windows_wsl_fallback=False,
    )

    prompt = str(captured["prompt"])
    assert prompt.strip()
    assert "Review this implementation slice" in prompt
    assert "Reviewer output is advisory risk input" in prompt
    assert "=== BEGIN DIFF ===" in prompt
    assert dict(captured["review_scope"])["manual_prompt_mode"] is True
    assert result["reviewed_head"] == head
    assert result["output_refs"] == ["rollout://alpha"]
    assert anchor_calls == []


def test_create_resume_and_id_reprint_use_one_pending_action(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    deslop_calls = _stub_deslop(monkeypatch)
    review_calls = _stub_review(monkeypatch, "phase_review-round-1", "phase_review-round-2")
    gate_calls = _stub_gate(monkeypatch, "phase_gate-round-1")
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")
    _git(repo, "checkout", "-b", "feature/review-shell")
    _commit_file(repo, "app.txt", "feature\n", "feature")

    args = ["--mode", "normal", "--cd", str(repo), "--base", "main", "--state-dir", str(state_dir)]
    exit_code, payload = _run_review(monkeypatch, args)
    public_id = str(payload["review"])

    assert exit_code == 0
    assert public_id.startswith("rvw_")
    assert payload["stage"] == "created"
    assert payload["mode"] == "normal"
    assert payload["selection"] == "stable"
    assert "grading" not in payload
    assert set(dict(payload["Action"])) == {"cmd"}
    assert f"--id {public_id}" in str(payload["Action"]["cmd"])
    assert "--decision" not in str(payload["Action"]["cmd"])
    assert len(deslop_calls) == 1
    state = _cycle_payload(state_dir, public_id)
    assert state["deslop"]["status"] == "done"
    assert state["rounds"] == []

    exit_code, resumed = _run_review(monkeypatch, args)
    assert exit_code == 0
    assert resumed["review"] == public_id
    assert resumed["stage"] == "decision-pending"
    assert set(dict(resumed["Action"])) == {"cmd", "alt"}
    assert "--decision clean" in str(resumed["Action"]["cmd"])
    assert "--decision findings" in str(resumed["Action"]["alt"])
    assert len(list((state_dir / "orchestrator" / "cycles").glob("*.json"))) == 1
    state = _cycle_payload(state_dir, public_id)
    assert len(state["rounds"]) == 1
    assert state["rounds"][0]["round_id"] == "phase_review-round-1"
    assert state["rounds"][0]["lane"] == "review_t1"
    assert state["rounds"][0]["review_status"] == "completed"
    assert state["rounds"][0]["output_refs"] == ["rollout://phase_review-round-1/alpha"]
    assert len(deslop_calls) == 1
    assert len(review_calls) == 1

    exit_code, by_id = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert by_id["review"] == public_id
    assert by_id["Action"] == resumed["Action"]
    assert len(_cycle_payload(state_dir, public_id)["rounds"]) == 1
    assert len(review_calls) == 1

    exit_code, first_clean = _run_review(monkeypatch, ["--id", public_id, "--decision", "clean", "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert first_clean["review"] == public_id
    assert first_clean["stage"] == "created"
    assert set(dict(first_clean["Action"])) == {"cmd"}
    assert f"--id {public_id}" in str(first_clean["Action"]["cmd"])
    assert "--decision" not in str(first_clean["Action"]["cmd"])
    state = _cycle_payload(state_dir, public_id)
    assert state["pending_action"] == {"kind": "run-review-step", "step_index": 1, "step": "precision-signoff"}
    assert state["review_progress"]["completed_steps"] == [
        {
            "index": 0,
            "name": "broad-discovery",
            "round_id": "phase_review-round-1",
            "lane": "review_t1",
            "reviewed_head": state["rounds"][0]["reviewed_head"],
        }
    ]
    assert len(review_calls) == 1

    exit_code, second_step = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert second_step["review"] == public_id
    assert second_step["stage"] == "decision-pending"
    assert "--decision clean" in str(second_step["Action"]["cmd"])
    assert len(review_calls) == 2
    assert review_calls[0]["step_name"] == "broad-discovery"
    assert review_calls[1]["step_name"] == "precision-signoff"
    state = _cycle_payload(state_dir, public_id)
    assert len(state["rounds"]) == 2
    assert state["rounds"][1]["round_id"] == "phase_review-round-2"

    exit_code, gate_queued = _run_review(monkeypatch, ["--id", public_id, "--decision", "clean", "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert gate_queued["stage"] == "created"
    assert f"--id {public_id}" in str(gate_queued["Action"]["cmd"])
    assert "--decision" not in str(gate_queued["Action"]["cmd"])
    state = _cycle_payload(state_dir, public_id)
    assert state["pending_action"] == {
        "kind": "run-review-step",
        "step_index": 2,
        "step": "local-signoff",
        "step_kind": "gate",
        "gate": "phase_gate",
    }
    assert [item["round_id"] for item in state["review_progress"]["completed_steps"]] == [
        "phase_review-round-1",
        "phase_review-round-2",
    ]
    assert len(gate_calls) == 0

    exit_code, gate_step = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert gate_step["stage"] == "decision-pending"
    assert "--decision clean" in str(gate_step["Action"]["cmd"])
    assert len(gate_calls) == 1
    assert gate_calls[0]["gate_task_class"] == "phase_gate"
    state = _cycle_payload(state_dir, public_id)
    assert len(state["rounds"]) == 3
    assert state["rounds"][2]["round_id"] == "phase_gate-round-1"
    assert state["rounds"][2]["kind"] == "gate"
    assert state["rounds"][2]["gate"] == "phase_gate"
    assert state["rounds"][2]["signoff_required"] is True

    exit_code, final_clean = _run_review(monkeypatch, ["--id", public_id, "--decision", "clean", "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert final_clean["stage"] == "review-green"
    _assert_github_handoff(final_clean["Action"], blocked_by=["full_suite:unknown", "ci:unknown"])
    state = _cycle_payload(state_dir, public_id)
    assert state["pending_action"] is None
    assert state["validation"]["review_green"] == "passed"
    assert [item["round_id"] for item in state["review_progress"]["completed_steps"]] == [
        "phase_review-round-1",
        "phase_review-round-2",
        "phase_gate-round-1",
    ]
    decisions = _gate_signoff_decisions(state_dir)
    assert [(item["round_id"], item["verdict"], item["workflow_anchor_recorded"]) for item in decisions] == [
        ("phase_gate-round-1", "clean", True)
    ]

    exit_code, pending_validation = _run_review(
        monkeypatch,
        [
            "--id",
            public_id,
            "--focused-validation",
            "passed",
            "--full-suite",
            "pending",
            "--ci",
            "pending",
            "--state-dir",
            str(state_dir),
        ],
    )
    assert exit_code == 0
    assert pending_validation["stage"] == "review-green"
    _assert_github_handoff(pending_validation["Action"], blocked_by=["full_suite:pending", "ci:pending"])
    state = _cycle_payload(state_dir, public_id)
    assert state["validation"]["focused"] == "passed"
    assert state["validation"]["full_suite"] == "pending"
    assert state["validation"]["ci"] == "pending"
    assert len(deslop_calls) == 1
    assert len(review_calls) == 2

    exit_code, validation_ready = _run_review(
        monkeypatch,
        [
            "--id",
            public_id,
            "--full-suite",
            "passed",
            "--ci",
            "classified",
            "--state-dir",
            str(state_dir),
        ],
    )
    assert exit_code == 0
    _assert_github_handoff(validation_ready["Action"], blocked_by=[])
    state = _cycle_payload(state_dir, public_id)
    assert state["validation"]["full_suite"] == "passed"
    assert state["validation"]["ci"] == "classified"
    assert len(deslop_calls) == 1
    assert len(review_calls) == 2
    assert len(gate_calls) == 1


def test_findings_fix_progression_and_clean_decision(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_deslop(monkeypatch)
    review_calls = _stub_review(monkeypatch, "phase_review-round-1", "phase_review-round-2")
    followup_calls = _stub_followup(monkeypatch, "followup-round-1")
    gate_calls = _stub_gate(monkeypatch, "phase_gate-round-1")
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")
    _git(repo, "checkout", "-b", "feature/findings")
    _commit_file(repo, "app.txt", "feature\n", "feature")

    _, created = _run_review(
        monkeypatch,
        ["--mode", "normal", "--cd", str(repo), "--base", "main", "--state-dir", str(state_dir)],
    )
    public_id = str(created["review"])

    _, opened = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    assert opened["stage"] == "decision-pending"

    exit_code, findings = _run_review(monkeypatch, ["--id", public_id, "--decision", "findings", "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert findings["stage"] == "fix-pending"
    assert findings["Action"]["note"] == "Fix valid findings, then rerun this command."
    assert f"--id {public_id}" in str(findings["Action"]["cmd"])
    assert "--decision" not in str(findings["Action"]["cmd"])
    state = _cycle_payload(state_dir, public_id)
    assert state["active_findings"]["round_id"] == "phase_review-round-1"
    assert len(state["decisions"]) == 1

    exit_code, reprint = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert reprint["stage"] == "fix-pending"
    assert len(_cycle_payload(state_dir, public_id)["rounds"]) == 1
    assert len(followup_calls) == 0

    _commit_file(repo, "app.txt", "fixed\n", "fix findings")
    exit_code, followup = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert followup["stage"] == "decision-pending"
    assert "--decision clean" in str(followup["Action"]["cmd"])
    assert len(followup_calls) == 1
    assert "Review this follow-up diff" in str(followup_calls[0]["prompt"])
    assert followup_calls[0]["review_scope"]["source_round_id"] == "phase_review-round-1"
    state = _cycle_payload(state_dir, public_id)
    assert len(state["rounds"]) == 2
    assert state["rounds"][1]["lane"] == "review-followup"
    assert state["rounds"][1]["round_id"] == "followup-round-1"
    assert state["rounds"][1]["source_round_id"] == "phase_review-round-1"
    assert state["rounds"][1]["output_refs"] == ["rollout://followup-round-1/alpha"]

    exit_code, followup_reprint = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert followup_reprint["Action"] == followup["Action"]
    assert len(followup_calls) == 1

    exit_code, clean = _run_review(monkeypatch, ["--id", public_id, "--decision", "clean", "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert clean["stage"] == "created"
    assert f"--id {public_id}" in str(clean["Action"]["cmd"])
    state = _cycle_payload(state_dir, public_id)
    assert state["active_findings"] is None
    assert state["validation"]["review_green"] == "unknown"
    assert state["pending_action"] == {"kind": "run-review-step", "step_index": 1, "step": "precision-signoff"}
    assert state["review_progress"]["completed_steps"] == [
        {
            "index": 0,
            "name": "broad-discovery",
            "round_id": "phase_review-round-1",
            "lane": "review_t1",
            "reviewed_head": state["rounds"][1]["reviewed_head"],
        }
    ]

    exit_code, second_step = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert second_step["stage"] == "decision-pending"
    assert len(review_calls) == 2
    assert review_calls[1]["step_name"] == "precision-signoff"

    exit_code, gate_queued = _run_review(monkeypatch, ["--id", public_id, "--decision", "clean", "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert gate_queued["stage"] == "created"
    assert f"--id {public_id}" in str(gate_queued["Action"]["cmd"])
    state = _cycle_payload(state_dir, public_id)
    assert state["pending_action"] == {
        "kind": "run-review-step",
        "step_index": 2,
        "step": "local-signoff",
        "step_kind": "gate",
        "gate": "phase_gate",
    }
    assert len(gate_calls) == 0

    exit_code, gate_step = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert gate_step["stage"] == "decision-pending"
    assert len(gate_calls) == 1

    exit_code, final_clean = _run_review(monkeypatch, ["--id", public_id, "--decision", "clean", "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert final_clean["stage"] == "review-green"
    _assert_github_handoff(final_clean["Action"], blocked_by=["full_suite:unknown", "ci:unknown"])
    state = _cycle_payload(state_dir, public_id)
    assert state["active_findings"] is None
    assert state["validation"]["review_green"] == "passed"
    assert [item["round_id"] for item in state["review_progress"]["completed_steps"]] == [
        "phase_review-round-1",
        "phase_review-round-2",
        "phase_gate-round-1",
    ]
    assert _gate_signoff_decisions(state_dir)[0]["verdict"] == "clean"


def test_gate_findings_flow_requires_followup_and_same_gate_rerun(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_deslop(monkeypatch)
    _stub_review(monkeypatch, "phase_review-round-1", "phase_review-round-2")
    followup_calls = _stub_followup(monkeypatch, "followup-round-1")
    gate_calls = _stub_gate(monkeypatch, "phase_gate-round-1", "phase_gate-round-2")
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")
    _git(repo, "checkout", "-b", "feature/gate-findings")
    _commit_file(repo, "app.txt", "feature\n", "feature")

    _, created = _run_review(
        monkeypatch,
        ["--mode", "normal", "--cd", str(repo), "--base", "main", "--state-dir", str(state_dir)],
    )
    public_id = str(created["review"])
    _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    _run_review(monkeypatch, ["--id", public_id, "--decision", "clean", "--state-dir", str(state_dir)])
    _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    _run_review(monkeypatch, ["--id", public_id, "--decision", "clean", "--state-dir", str(state_dir)])
    _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])

    exit_code, findings = _run_review(monkeypatch, ["--id", public_id, "--decision", "findings", "--state-dir", str(state_dir)])

    assert exit_code == 0
    assert findings["stage"] == "fix-pending"
    assert len(gate_calls) == 1
    state = _cycle_payload(state_dir, public_id)
    assert state["active_findings"]["gate"]["gate"] == "phase_gate"
    assert state["active_findings"]["status"] == "fix-pending"
    assert _gate_signoff_decisions(state_dir)[0]["verdict"] == "findings"

    _commit_file(repo, "app.txt", "fixed gate finding\n", "fix gate finding")
    exit_code, followup = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert followup["stage"] == "decision-pending"
    assert len(followup_calls) == 1

    exit_code, rerun_needed = _run_review(monkeypatch, ["--id", public_id, "--decision", "clean", "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert rerun_needed["stage"] == "gate-rerun-needed"
    state = _cycle_payload(state_dir, public_id)
    assert state["pending_action"]["kind"] == "rerun-gate"
    assert state["pending_action"]["gate"] == "phase_gate"

    exit_code, gate_rerun = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert gate_rerun["stage"] == "decision-pending"
    assert len(gate_calls) == 2
    state = _cycle_payload(state_dir, public_id)
    assert state["pending_action"]["round_id"] == "phase_gate-round-2"
    assert state["active_findings"]["status"] == "decision-pending"

    exit_code, clean = _run_review(monkeypatch, ["--id", public_id, "--decision", "clean", "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert clean["stage"] == "review-green"
    state = _cycle_payload(state_dir, public_id)
    assert state["active_findings"] is None
    assert state["resolved_gate_findings"][0]["source_round_id"] == "phase_gate-round-1"
    assert state["resolved_gate_findings"][0]["rerun_round_id"] == "phase_gate-round-2"
    assert [item["verdict"] for item in _gate_signoff_decisions(state_dir)] == ["findings", "clean"]


def test_followup_findings_loops_back_to_fix_pending(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_deslop(monkeypatch)
    _stub_review(monkeypatch, "phase_review-round-1")
    followup_calls = _stub_followup(monkeypatch, "followup-round-1")
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")
    _git(repo, "checkout", "-b", "feature/followup-findings")
    _commit_file(repo, "app.txt", "feature\n", "feature")

    _, created = _run_review(
        monkeypatch,
        ["--mode", "normal", "--cd", str(repo), "--base", "main", "--state-dir", str(state_dir)],
    )
    public_id = str(created["review"])
    _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    _run_review(monkeypatch, ["--id", public_id, "--decision", "findings", "--state-dir", str(state_dir)])
    _commit_file(repo, "app.txt", "fixed once\n", "fix findings")

    _, followup = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    assert followup["stage"] == "decision-pending"
    assert len(followup_calls) == 1

    exit_code, findings = _run_review(monkeypatch, ["--id", public_id, "--decision", "findings", "--state-dir", str(state_dir)])

    assert exit_code == 0
    assert findings["stage"] == "fix-pending"
    assert findings["Action"]["note"] == "Fix valid findings, then rerun this command."
    state = _cycle_payload(state_dir, public_id)
    assert state["active_findings"]["round_id"] == "followup-round-1"
    assert state["active_findings"]["lane"] == "review-followup"
    assert state["active_findings"]["previous_round_id"] == "phase_review-round-1"
    assert state["active_findings"]["status"] == "fix-pending"
    assert state["validation"]["review_green"] == "unknown"

    exit_code, reprint = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert reprint["stage"] == "fix-pending"
    assert len(followup_calls) == 1


def test_validation_flags_do_not_run_expensive_resume(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    deslop_calls = _stub_deslop(monkeypatch)
    review_calls = _stub_review(monkeypatch, "phase_review-round-1")
    followup_calls = _stub_followup(monkeypatch, "followup-round-1")
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")
    _git(repo, "checkout", "-b", "feature/validation-only")
    _commit_file(repo, "app.txt", "feature\n", "feature")

    _, created = _run_review(
        monkeypatch,
        ["--mode", "normal", "--cd", str(repo), "--base", "main", "--state-dir", str(state_dir)],
    )
    public_id = str(created["review"])
    _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    _run_review(monkeypatch, ["--id", public_id, "--decision", "findings", "--state-dir", str(state_dir)])
    _commit_file(repo, "app.txt", "fixed\n", "fix findings")

    exit_code, payload = _run_review(
        monkeypatch,
        ["--id", public_id, "--full-suite", "pending", "--state-dir", str(state_dir)],
    )

    assert exit_code == 0
    assert payload["stage"] == "fix-pending"
    assert len(deslop_calls) == 1
    assert len(review_calls) == 1
    assert followup_calls == []
    state = _cycle_payload(state_dir, public_id)
    assert state["validation"]["full_suite"] == "pending"
    assert state["active_findings"]["round_id"] == "phase_review-round-1"


def test_benchmark_selection_prints_grading_requirement(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_deslop(monkeypatch)
    _stub_review(monkeypatch)
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")

    exit_code, payload = _run_review(
        monkeypatch,
        [
            "--mode",
            "normal",
            "--selection",
            "benchmark",
            "--cd",
            str(repo),
            "--base",
            "main",
            "--state-dir",
            str(state_dir),
        ],
    )

    assert exit_code == 0
    assert payload["selection"] == "benchmark"
    assert payload["grading"] == "required"


def test_emergency_mode_skips_deslop_and_runs_review(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    review_calls = _stub_review(monkeypatch)
    gate_calls = _stub_gate(monkeypatch, "phase_gate-emergency-1")

    def fail_run(*, command: list[str], cwd: Path) -> subprocess.CompletedProcess:
        raise AssertionError("emergency mode must not run deslop")

    monkeypatch.setattr(orchestrator_runner, "run_deslop_subprocess", fail_run)
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")

    exit_code, payload = _run_review(
        monkeypatch,
        ["--mode", "emergency", "--cd", str(repo), "--base", "main", "--state-dir", str(state_dir)],
    )

    assert exit_code == 0
    assert payload["stage"] == "decision-pending"
    assert set(dict(payload["Action"])) == {"cmd", "alt"}
    assert len(review_calls) == 1
    public_id = str(payload["review"])

    exit_code, gate_queued = _run_review(monkeypatch, ["--id", public_id, "--decision", "clean", "--state-dir", str(state_dir)])

    assert exit_code == 0
    assert gate_queued["stage"] == "created"
    assert len(gate_calls) == 0

    exit_code, gate_step = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert gate_step["stage"] == "decision-pending"
    assert len(gate_calls) == 1

    exit_code, clean = _run_review(monkeypatch, ["--id", public_id, "--decision", "clean", "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert clean["stage"] == "review-green"
    _assert_github_handoff(clean["Action"], blocked_by=["full_suite:unknown", "ci:unknown"])
    assert len(review_calls) == 1
    assert _gate_signoff_decisions(state_dir)[0]["round_id"] == "phase_gate-emergency-1"


def test_deslop_failure_retries_before_review(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    deslop_calls = _stub_deslop(monkeypatch, 9, 0)
    review_calls = _stub_review(monkeypatch)
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")
    _git(repo, "checkout", "-b", "feature/deslop-retry")
    _commit_file(repo, "app.txt", "feature\n", "feature")

    exit_code, failed = _run_review(
        monkeypatch,
        ["--mode", "normal", "--cd", str(repo), "--base", "main", "--state-dir", str(state_dir)],
    )
    public_id = str(failed["review"])

    assert exit_code == 0
    assert failed["stage"] == "retry-requested"
    assert f"--id {public_id}" in str(failed["Action"]["cmd"])
    state = _cycle_payload(state_dir, public_id)
    assert state["deslop"]["status"] == "failed"
    assert state["recovery"]["status"] == "retry-requested"
    assert state["rounds"] == []

    exit_code, retried = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert retried["stage"] == "created"
    assert len(deslop_calls) == 2
    assert _cycle_payload(state_dir, public_id)["deslop"]["status"] == "done"

    exit_code, resumed = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert resumed["stage"] == "decision-pending"
    assert len(deslop_calls) == 2
    assert len(review_calls) == 1
