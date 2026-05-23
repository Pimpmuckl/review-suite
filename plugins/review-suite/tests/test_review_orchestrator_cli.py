from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import review
import review_suite_arena
from review_suite_core import orchestrator_runner


@pytest.fixture(autouse=True)
def _isolate_default_state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(review, "default_state_dir", lambda: tmp_path / "default-state")


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


def _write_cycle_payload(state_dir: Path, public_id: str, payload: dict[str, object]) -> None:
    index = json.loads((state_dir / "orchestrator" / "index.json").read_text(encoding="utf-8"))
    cycle_key = index["ids"][public_id]
    (state_dir / "orchestrator" / "cycles" / f"{cycle_key}.json").write_text(json.dumps(payload), encoding="utf-8")


def _gate_signoff_decisions(state_dir: Path) -> list[dict[str, object]]:
    path = state_dir / "gate_signoffs.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _assert_github_handoff(action: object, *, public_id: str, state_dir: Path, blocked_by: list[str]) -> None:
    assert isinstance(action, dict)
    payload = dict(action)
    assert "kind" not in payload
    assert "lane" not in payload
    assert payload["after"] == "PR create/update"
    assert "github_review" not in payload
    assert "merge_ready" not in payload
    cmd = str(payload["cmd"])
    assert f"--id {public_id}" in cmd
    assert "--github-review" in cmd
    assert "--state-dir" not in cmd
    assert str(state_dir) not in cmd
    assert "--github-force" not in cmd
    if blocked_by:
        assert payload["blocked_by"] == blocked_by
    else:
        assert "validation_ready" not in payload
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
    banner_calls: list[dict[str, object]] = []
    refresh_calls: list[dict[str, object]] = []

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
    monkeypatch.setattr(review_suite_arena, "_print_round_banner", lambda **kwargs: banner_calls.append(kwargs))
    monkeypatch.setattr(review_suite_arena, "_print_findings", lambda result: False)
    monkeypatch.setattr(review_suite_arena, "record_review_anchor", lambda **kwargs: anchor_calls.append(kwargs))
    monkeypatch.setattr(review_suite_arena, "refresh_review_cost_report_best_effort", lambda **kwargs: refresh_calls.append(kwargs))

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
        step_position=1,
        step_total=2,
    )

    prompt = str(captured["prompt"])
    assert prompt.strip()
    assert "Review this implementation slice" in prompt
    assert "Reviewer output is advisory risk input" in prompt
    assert "=== BEGIN DIFF ===" in prompt
    assert dict(captured["review_scope"])["manual_prompt_mode"] is True
    assert result["reviewed_head"] == head
    assert result["output_refs"] == ["rollout://alpha"]
    assert banner_calls == [{"task_name": "review 1/2 precision", "round_id": result["round_id"]}]
    assert anchor_calls == []
    assert refresh_calls == [{"state_dir": state_dir, "review_cwd": repo}]


def test_create_resume_and_id_reprint_use_one_pending_action(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    deslop_calls = _stub_deslop(monkeypatch)
    review_calls = _stub_review(monkeypatch, "phase_review-round-1", "phase_review-round-2")
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
    assert "stage" not in payload
    assert "mode" not in payload
    assert "selection" not in payload
    assert "grading" not in payload
    assert set(dict(payload["Action"])) == {"cmd"}
    assert f"--id {public_id}" in str(payload["Action"]["cmd"])
    assert "--decision" not in str(payload["Action"]["cmd"])
    assert len(deslop_calls) == 1
    locator = json.loads((tmp_path / "default-state" / "orchestrator" / "state_dirs.json").read_text(encoding="utf-8"))
    assert locator["ids"][public_id] == str(state_dir.resolve())
    state = _cycle_payload(state_dir, public_id)
    assert state["selection"] == {
        "requested": "auto",
        "effective": "stable",
        "reason": "auto_stable_profile",
    }
    assert state["grading"] == {"required": False}
    assert state["deslop"]["status"] == "done"
    assert state["rounds"] == []

    exit_code, resumed = _run_review(monkeypatch, args)
    assert exit_code == 0
    assert resumed["review"] == public_id
    assert "stage" not in resumed
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

    exit_code, by_id = _run_review(monkeypatch, ["--id", public_id])
    assert exit_code == 0
    assert by_id["review"] == public_id
    assert by_id["Action"] == resumed["Action"]
    assert len(_cycle_payload(state_dir, public_id)["rounds"]) == 1
    assert len(review_calls) == 1

    exit_code, first_clean = _run_review(monkeypatch, ["--id", public_id, "--decision", "clean"])
    assert exit_code == 0
    assert first_clean["review"] == public_id
    assert "stage" not in first_clean
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
    assert "stage" not in second_step
    assert "--decision clean" in str(second_step["Action"]["cmd"])
    assert len(review_calls) == 2
    assert review_calls[0]["step_name"] == "broad-discovery"
    assert review_calls[0]["step_position"] == 1
    assert review_calls[0]["step_total"] == 2
    assert review_calls[1]["step_name"] == "precision-signoff"
    assert review_calls[1]["step_position"] == 2
    assert review_calls[1]["step_total"] == 2
    state = _cycle_payload(state_dir, public_id)
    assert len(state["rounds"]) == 2
    assert state["rounds"][1]["round_id"] == "phase_review-round-2"

    exit_code, final_clean = _run_review(monkeypatch, ["--id", public_id, "--decision", "clean", "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert "stage" not in final_clean
    _assert_github_handoff(
        final_clean["Action"],
        public_id=public_id,
        state_dir=state_dir,
        blocked_by=["full_suite:unknown", "ci:unknown"],
    )
    state = _cycle_payload(state_dir, public_id)
    assert state["pending_action"] is None
    assert state["validation"]["review_green"] == "passed"
    assert [item["round_id"] for item in state["review_progress"]["completed_steps"]] == [
        "phase_review-round-1",
        "phase_review-round-2",
    ]
    assert _gate_signoff_decisions(state_dir) == []

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
    assert "stage" not in pending_validation
    _assert_github_handoff(
        pending_validation["Action"],
        public_id=public_id,
        state_dir=state_dir,
        blocked_by=["full_suite:pending", "ci:pending"],
    )
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
    _assert_github_handoff(validation_ready["Action"], public_id=public_id, state_dir=state_dir, blocked_by=[])
    state = _cycle_payload(state_dir, public_id)
    assert state["validation"]["full_suite"] == "passed"
    assert state["validation"]["ci"] == "classified"
    assert len(deslop_calls) == 1
    assert len(review_calls) == 2


def test_restart_mode_supersedes_cycle_and_starts_fresh_deep_ladder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deslop_calls = _stub_deslop(monkeypatch)
    review_calls = _stub_review(monkeypatch, "normal-round-1", "deep-round-1")
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")
    _git(repo, "checkout", "-b", "feature/restart-deep")
    _commit_file(repo, "app.txt", "feature\n", "feature")

    _, created = _run_review(
        monkeypatch,
        ["--mode", "normal", "--cd", str(repo), "--base", "main", "--state-dir", str(state_dir)],
    )
    old_id = str(created["review"])
    _run_review(monkeypatch, ["--id", old_id, "--state-dir", str(state_dir)])

    exit_code, restarted = _run_review(
        monkeypatch,
        [
            "--id",
            old_id,
            "--restart-mode",
            "deep",
            "--reason",
            "github review had many suspicious notes",
            "--state-dir",
            str(state_dir),
        ],
    )

    new_id = str(restarted["review"])
    assert exit_code == 0
    assert new_id != old_id
    assert f"--id {new_id}" in str(restarted["Action"]["cmd"])
    assert len(deslop_calls) == 2
    assert len(review_calls) == 1
    assert len(list((state_dir / "orchestrator" / "cycles").glob("*.json"))) == 2

    old_state = _cycle_payload(state_dir, old_id)
    new_state = _cycle_payload(state_dir, new_id)
    assert old_state["stage"] == "aborted"
    assert old_state["recovery"]["status"] == "aborted"
    assert old_state["superseded_by"] == {
        "review": new_id,
        "cycle_key": new_state["cycle_key"],
        "mode": "deep",
        "reason": "github review had many suspicious notes",
    }
    assert new_state["mode"] == {"requested": "deep", "effective": "deep"}
    assert new_state["identity"] == old_state["identity"]
    assert new_state["cycle_key"] != old_state["cycle_key"]
    assert new_state["restart"]["token"] == f"{old_state['cycle_key']}:deep"
    assert new_state["restart"]["supersedes"] == old_id
    assert new_state["restart"]["supersedes_cycle_key"] == old_state["cycle_key"]
    assert new_state["restart"]["from_mode"] == "normal"
    assert [step["name"] for step in new_state["review_plan"]["steps"]] == [
        "broad-discovery",
        "deep-discovery",
        "deep-signoff",
    ]

    old_state_without_redirect = dict(old_state)
    old_state_without_redirect.pop("superseded_by")
    _write_cycle_payload(state_dir, old_id, old_state_without_redirect)
    _, retry = _run_review(
        monkeypatch,
        [
            "--id",
            old_id,
            "--restart-mode",
            "deep",
            "--reason",
            "retry after partial write",
            "--state-dir",
            str(state_dir),
        ],
    )
    assert retry["review"] == new_id
    assert len(deslop_calls) == 2
    assert len(review_calls) == 1

    _, old_reprint = _run_review(monkeypatch, ["--id", old_id, "--state-dir", str(state_dir)])
    assert f"--id {new_id}" in str(old_reprint["Action"]["cmd"])
    assert "superseded" in str(old_reprint["Action"]["note"])

    _, deep_review = _run_review(monkeypatch, ["--id", new_id, "--state-dir", str(state_dir)])
    assert "--decision clean" in str(deep_review["Action"]["cmd"])
    assert len(review_calls) == 2
    assert review_calls[1]["step_name"] == "broad-discovery"
    assert review_calls[1]["step_position"] == 1
    assert review_calls[1]["step_total"] == 3


def test_restart_mode_requires_escalation_reason_and_stricter_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub_deslop(monkeypatch)
    _stub_review(monkeypatch)
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")

    _, created = _run_review(
        monkeypatch,
        ["--mode", "normal", "--cd", str(repo), "--base", "main", "--state-dir", str(state_dir)],
    )
    public_id = str(created["review"])

    messages: list[str] = []
    monkeypatch.setattr(review, "emit_error", lambda message, **kwargs: messages.append(str(message)) or 2)

    monkeypatch.setattr(sys, "argv", ["review.py", "--id", public_id, "--restart-mode", "deep", "--state-dir", str(state_dir)])
    assert review.main() == 2
    assert messages[-1] == "--reason is required for --restart-mode"

    monkeypatch.setattr(
        sys,
        "argv",
        ["review.py", "--id", public_id, "--restart-mode", "brief", "--reason", "try downgrade", "--state-dir", str(state_dir)],
    )
    assert review.main() == 2
    assert "--restart-mode must increase strictness" in messages[-1]


def test_restart_mode_rejects_dirty_worktree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub_deslop(monkeypatch)
    _stub_review(monkeypatch)
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")

    _, created = _run_review(
        monkeypatch,
        ["--mode", "normal", "--cd", str(repo), "--base", "main", "--state-dir", str(state_dir)],
    )
    public_id = str(created["review"])
    (repo / "app.txt").write_text("dirty\n", encoding="utf-8")

    messages: list[str] = []
    monkeypatch.setattr(review, "emit_error", lambda message, **kwargs: messages.append(str(message)) or 2)
    monkeypatch.setattr(
        sys,
        "argv",
        ["review.py", "--id", public_id, "--restart-mode", "deep", "--reason", "rerun deeper", "--state-dir", str(state_dir)],
    )

    assert review.main() == 2
    assert messages[-1] == "cannot restart review cycle with a dirty worktree; commit or stash changes, then rerun"
    assert "superseded_by" not in _cycle_payload(state_dir, public_id)
    assert len(list((state_dir / "orchestrator" / "cycles").glob("*.json"))) == 1


def test_restart_mode_rejects_changed_head(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub_deslop(monkeypatch)
    _stub_review(monkeypatch)
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")

    _, created = _run_review(
        monkeypatch,
        ["--mode", "normal", "--cd", str(repo), "--base", "main", "--state-dir", str(state_dir)],
    )
    public_id = str(created["review"])
    _commit_file(repo, "app.txt", "changed head\n", "change head")

    messages: list[str] = []
    monkeypatch.setattr(review, "emit_error", lambda message, **kwargs: messages.append(str(message)) or 2)
    monkeypatch.setattr(
        sys,
        "argv",
        ["review.py", "--id", public_id, "--restart-mode", "deep", "--reason", "rerun deeper", "--state-dir", str(state_dir)],
    )

    assert review.main() == 2
    assert messages[-1] == "cannot restart review cycle after HEAD changed; start a new review instead"
    assert "superseded_by" not in _cycle_payload(state_dir, public_id)
    assert len(list((state_dir / "orchestrator" / "cycles").glob("*.json"))) == 1


def test_review_orchestrator_help_hides_internal_selection() -> None:
    help_text = review.build_parser().format_help()

    assert "--mode" in help_text
    assert "--restart-mode" in help_text
    assert "--selection" not in help_text
    assert "--state-dir" not in help_text


def test_deslop_step_prints_output_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[list[str]] = []

    def fake_run(*, command: list[str], cwd: Path) -> subprocess.CompletedProcess:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="Remove redundant helper.\n", stderr="")

    monkeypatch.setattr(orchestrator_runner, "run_deslop_subprocess", fake_run)
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")

    exit_code, payload = _run_review(
        monkeypatch,
        ["--mode", "normal", "--cd", str(repo), "--base", "main", "--state-dir", str(state_dir)],
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "stage" not in payload
    assert captured.out.count("Output:") == 1
    assert "review-deslop:" in captured.out
    assert "Remove redundant helper." in captured.out
    assert "--output-only" in calls[0]


def test_deslop_failure_prints_output_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_run(*, command: list[str], cwd: Path) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(command, 9, stdout="Could not complete deslop.\n", stderr="")

    monkeypatch.setattr(orchestrator_runner, "run_deslop_subprocess", fake_run)
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")

    exit_code, payload = _run_review(
        monkeypatch,
        ["--mode", "normal", "--cd", str(repo), "--base", "main", "--state-dir", str(state_dir)],
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "stage" not in payload
    assert captured.out.count("Output:") == 1
    assert "review-deslop [failed]:" in captured.out
    assert "Could not complete deslop." in captured.out


def test_review_step_output_is_not_reprinted_by_review_py(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    review_calls: list[dict[str, object]] = []

    def fake_run(**kwargs: object) -> dict[str, object]:
        review_calls.append(dict(kwargs))
        print("Output:\nalpha:\nReviewer body.")
        return {
            "round_id": "phase_review-round-1",
            "lane": "review_t1",
            "kind": "review",
            "status": "completed",
            "blocked": False,
            "reviewed_head": "head-1",
            "output_refs": ["rollout://phase_review-round-1/alpha"],
            "runs": [
                {
                    "slot": "alpha",
                    "status": "completed",
                    "summary": "Reviewer body.",
                    "ref": "rollout://phase_review-round-1/alpha",
                    "blocked": False,
                    "block": None,
                }
            ],
        }

    monkeypatch.setattr(orchestrator_runner, "run_review_step", fake_run)
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")

    exit_code, payload = _run_review(
        monkeypatch,
        ["--mode", "emergency", "--cd", str(repo), "--base", "main", "--state-dir", str(state_dir)],
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "stage" not in payload
    assert captured.out.count("Output:") == 1
    assert "Reviewer body." in captured.out
    assert len(review_calls) == 1


def test_id_rejects_creation_context_flags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_deslop(monkeypatch)
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")

    _, created = _run_review(
        monkeypatch,
        ["--mode", "normal", "--cd", str(repo), "--base", "main", "--state-dir", str(state_dir)],
    )
    errors: list[tuple[str, dict[str, object]]] = []

    def fake_error(message: str, **kwargs: object) -> int:
        errors.append((message, dict(kwargs)))
        return 2

    monkeypatch.setattr(review, "emit_error", fake_error)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "review.py",
            "--id",
            str(created["review"]),
            "--mode",
            "deep",
            "--cd",
            str(repo),
            "--base",
            "main",
        ],
    )

    exit_code = review.main()

    assert exit_code == 2
    assert len(errors) == 1
    message = errors[0][0]
    assert "--id already selects review context" in message
    assert "remove --mode, --cd, --base" in message
    assert "mode normal" in message
    assert str(repo.resolve()) in message


def test_github_review_rejects_cycle_before_local_green(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_deslop(monkeypatch)
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")

    _, created = _run_review(
        monkeypatch,
        ["--mode", "normal", "--cd", str(repo), "--base", "main", "--state-dir", str(state_dir)],
    )
    errors: list[tuple[str, dict[str, object]]] = []

    def fake_error(message: str, **kwargs: object) -> int:
        errors.append((message, dict(kwargs)))
        return 2

    monkeypatch.setattr(review, "emit_error", fake_error)
    monkeypatch.setattr(sys, "argv", ["review.py", "--id", str(created["review"]), "--github-review", "--state-dir", str(state_dir)])

    exit_code = review.main()

    assert exit_code == 2
    assert errors == [
        (
            "--github-review requires local green review state",
            {
                "status": "usage_error",
                "help_items": [review._help_command()],
            },
        )
    ]


def test_github_review_runs_existing_lane_with_resolved_state_dir_and_force(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    review_calls = _stub_review(monkeypatch)
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")

    _, opened = _run_review(
        monkeypatch,
        ["--mode", "emergency", "--cd", str(repo), "--base", "main", "--state-dir", str(state_dir)],
    )
    public_id = str(opened["review"])
    _, clean = _run_review(monkeypatch, ["--id", public_id, "--decision", "clean", "--state-dir", str(state_dir)])
    assert "stage" not in clean

    calls: list[list[str]] = []

    def fake_subprocess_run(command: list[str], check: bool) -> subprocess.CompletedProcess:
        calls.append(command)
        return subprocess.CompletedProcess(command, 23)

    monkeypatch.setattr(review.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(
        sys,
        "argv",
        ["review.py", "--id", public_id, "--github-review", "--github-force"],
    )

    exit_code = review.main()

    assert exit_code == 23
    assert calls == [
        [
            sys.executable,
            str((SCRIPT_DIR / "review_github.py").resolve()),
            "run",
            "--cd",
            str(repo.resolve()),
            "--state-dir",
            str(state_dir),
            "--force",
        ]
    ]
    assert len(review_calls) == 1


def test_findings_fix_progression_and_clean_decision(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_deslop(monkeypatch)
    review_calls = _stub_review(monkeypatch, "phase_review-round-1", "phase_review-round-2")
    followup_calls = _stub_followup(monkeypatch, "followup-round-1")
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
    assert "stage" not in opened

    exit_code, findings = _run_review(monkeypatch, ["--id", public_id, "--decision", "findings", "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert "stage" not in findings
    assert findings["Action"]["note"] == "Fix valid findings, then rerun this command."
    assert f"--id {public_id}" in str(findings["Action"]["cmd"])
    assert "--decision" not in str(findings["Action"]["cmd"])
    state = _cycle_payload(state_dir, public_id)
    assert state["active_findings"]["round_id"] == "phase_review-round-1"
    assert len(state["decisions"]) == 1

    exit_code, reprint = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert "stage" not in reprint
    assert len(_cycle_payload(state_dir, public_id)["rounds"]) == 1
    assert len(followup_calls) == 0

    _commit_file(repo, "app.txt", "fixed\n", "fix findings")
    exit_code, followup = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert "stage" not in followup
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
    assert "stage" not in clean
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
    assert "stage" not in second_step
    assert len(review_calls) == 2
    assert review_calls[1]["step_name"] == "precision-signoff"
    assert review_calls[1]["step_position"] == 2
    assert review_calls[1]["step_total"] == 2

    exit_code, final_clean = _run_review(monkeypatch, ["--id", public_id, "--decision", "clean", "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert "stage" not in final_clean
    _assert_github_handoff(
        final_clean["Action"],
        public_id=public_id,
        state_dir=state_dir,
        blocked_by=["full_suite:unknown", "ci:unknown"],
    )
    state = _cycle_payload(state_dir, public_id)
    assert state["active_findings"] is None
    assert state["validation"]["review_green"] == "passed"
    assert [item["round_id"] for item in state["review_progress"]["completed_steps"]] == [
        "phase_review-round-1",
        "phase_review-round-2",
    ]
    assert _gate_signoff_decisions(state_dir) == []


def test_findings_fix_progression_keeps_related_dirty_work_in_fix_pending_on_same_head(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub_deslop(monkeypatch)
    _stub_review(monkeypatch, "phase_review-round-1", "phase_review-round-2")
    followup_calls = _stub_followup(monkeypatch, "followup-round-1")
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")
    _git(repo, "checkout", "-b", "feature/dirty-fix")
    reviewed_head = _commit_file(repo, "app.txt", "feature\n", "feature")

    _, created = _run_review(
        monkeypatch,
        ["--mode", "normal", "--cd", str(repo), "--base", "main", "--state-dir", str(state_dir)],
    )
    public_id = str(created["review"])
    _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    _run_review(monkeypatch, ["--id", public_id, "--decision", "findings", "--state-dir", str(state_dir)])
    (repo / "app.txt").write_text("feature\nfixed in worktree\n", encoding="utf-8")

    exit_code, reprint = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])

    assert exit_code == 0
    assert reprint["Action"]["note"] == "Fix valid findings, then rerun this command."
    assert len(followup_calls) == 0
    assert _git(repo, "rev-parse", "HEAD") == reviewed_head


def test_findings_fix_progression_keeps_unrelated_dirty_work_in_fix_pending(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub_deslop(monkeypatch)
    _stub_review(monkeypatch, "phase_review-round-1", "phase_review-round-2")
    followup_calls = _stub_followup(monkeypatch, "followup-round-1")
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")
    _git(repo, "checkout", "-b", "feature/dirty-new-file-fix")
    reviewed_head = _commit_file(repo, "app.txt", "feature\n", "feature")

    _, created = _run_review(
        monkeypatch,
        ["--mode", "normal", "--cd", str(repo), "--base", "main", "--state-dir", str(state_dir)],
    )
    public_id = str(created["review"])
    _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    _run_review(monkeypatch, ["--id", public_id, "--decision", "findings", "--state-dir", str(state_dir)])
    (repo / "tests" / "test_fix.py").parent.mkdir(parents=True)
    (repo / "tests" / "test_fix.py").write_text("def test_fix():\n    assert True\n", encoding="utf-8")

    exit_code, reprint = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])

    assert exit_code == 0
    assert reprint["Action"]["note"] == "Fix valid findings, then rerun this command."
    assert len(followup_calls) == 0
    assert _git(repo, "rev-parse", "HEAD") == reviewed_head


def test_signoff_findings_require_direct_clean_rerun(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_deslop(monkeypatch)
    review_calls = _stub_review(monkeypatch, "phase_review-round-1", "phase_review-round-2", "phase_review-round-3")
    followup_calls = _stub_followup(monkeypatch, "followup-round-1")
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")
    _git(repo, "checkout", "-b", "feature/signoff-rerun")
    _commit_file(repo, "app.txt", "feature\n", "feature")

    _, created = _run_review(
        monkeypatch,
        ["--mode", "normal", "--cd", str(repo), "--base", "main", "--state-dir", str(state_dir)],
    )
    public_id = str(created["review"])
    _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    _run_review(monkeypatch, ["--id", public_id, "--decision", "clean", "--state-dir", str(state_dir)])
    _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])

    exit_code, findings = _run_review(monkeypatch, ["--id", public_id, "--decision", "findings", "--state-dir", str(state_dir)])

    assert exit_code == 0
    assert "stage" not in findings
    assert len(review_calls) == 2
    assert review_calls[1]["step_name"] == "precision-signoff"

    _commit_file(repo, "app.txt", "fixed\n", "fix signoff finding")
    _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    exit_code, rerun_ready = _run_review(monkeypatch, ["--id", public_id, "--decision", "clean", "--state-dir", str(state_dir)])

    assert exit_code == 0
    assert "stage" not in rerun_ready
    state = _cycle_payload(state_dir, public_id)
    assert state["pending_action"] == {"kind": "run-review-step", "step_index": 1, "step": "precision-signoff"}
    assert [item["round_id"] for item in state["review_progress"]["completed_steps"]] == ["phase_review-round-1"]
    assert len(followup_calls) == 1

    exit_code, signoff_rerun = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])

    assert exit_code == 0
    assert "stage" not in signoff_rerun
    assert len(review_calls) == 3
    assert review_calls[2]["step_name"] == "precision-signoff"

    exit_code, green = _run_review(monkeypatch, ["--id", public_id, "--decision", "clean", "--state-dir", str(state_dir)])

    assert exit_code == 0
    assert "stage" not in green
    state = _cycle_payload(state_dir, public_id)
    assert [item["round_id"] for item in state["review_progress"]["completed_steps"]] == [
        "phase_review-round-1",
        "phase_review-round-3",
    ]


def test_gate_findings_flow_requires_followup_and_same_gate_rerun(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_deslop(monkeypatch)
    _stub_review(monkeypatch, "phase_review-round-1", "phase_review-round-2")
    followup_calls = _stub_followup(monkeypatch, "followup-round-1")
    gate_calls = _stub_gate(monkeypatch, "phase_gate-round-1", "phase_gate-round-2")
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    config = deepcopy(review.load_config(state_dir))
    config["orchestrator"]["profiles"]["stable"]["normal"]["steps"].append(
        {"name": "local-signoff", "kind": "gate", "gate": "phase_gate"}
    )
    monkeypatch.setattr(review, "load_config", lambda state_dir: config)
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
    assert "stage" not in findings
    assert len(gate_calls) == 1
    state = _cycle_payload(state_dir, public_id)
    assert state["active_findings"]["gate"]["gate"] == "phase_gate"
    assert state["active_findings"]["status"] == "fix-pending"
    assert _gate_signoff_decisions(state_dir)[0]["verdict"] == "findings"

    _commit_file(repo, "app.txt", "fixed gate finding\n", "fix gate finding")
    exit_code, followup = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert "stage" not in followup
    assert len(followup_calls) == 1

    exit_code, rerun_needed = _run_review(monkeypatch, ["--id", public_id, "--decision", "clean", "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert "stage" not in rerun_needed
    state = _cycle_payload(state_dir, public_id)
    assert state["pending_action"]["kind"] == "rerun-gate"
    assert state["pending_action"]["gate"] == "phase_gate"

    exit_code, gate_rerun = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert "stage" not in gate_rerun
    assert len(gate_calls) == 2
    state = _cycle_payload(state_dir, public_id)
    assert state["pending_action"]["round_id"] == "phase_gate-round-2"
    assert state["active_findings"]["status"] == "decision-pending"

    exit_code, clean = _run_review(monkeypatch, ["--id", public_id, "--decision", "clean", "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert "stage" not in clean
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
    assert "stage" not in followup
    assert len(followup_calls) == 1

    exit_code, findings = _run_review(monkeypatch, ["--id", public_id, "--decision", "findings", "--state-dir", str(state_dir)])

    assert exit_code == 0
    assert "stage" not in findings
    assert findings["Action"]["note"] == "Fix valid findings, then rerun this command."
    state = _cycle_payload(state_dir, public_id)
    assert state["active_findings"]["round_id"] == "followup-round-1"
    assert state["active_findings"]["lane"] == "review-followup"
    assert state["active_findings"]["previous_round_id"] == "phase_review-round-1"
    assert state["active_findings"]["status"] == "fix-pending"
    assert state["validation"]["review_green"] == "unknown"

    exit_code, reprint = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert "stage" not in reprint
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
    assert "stage" not in payload
    assert len(deslop_calls) == 1
    assert len(review_calls) == 1
    assert followup_calls == []
    state = _cycle_payload(state_dir, public_id)
    assert state["validation"]["full_suite"] == "pending"
    assert state["active_findings"]["round_id"] == "phase_review-round-1"


def test_benchmark_selection_keeps_only_grading_requirement_in_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_deslop(monkeypatch)
    _stub_review(monkeypatch)
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")
    config = deepcopy(review.load_config(state_dir))
    config["orchestrator"]["selection"] = "benchmark"
    monkeypatch.setattr(review, "load_config", lambda state_dir: config)

    exit_code, payload = _run_review(
        monkeypatch,
        [
            "--mode",
            "normal",
            "--cd",
            str(repo),
            "--base",
            "main",
            "--state-dir",
            str(state_dir),
        ],
    )

    assert exit_code == 0
    assert "selection" not in payload
    assert "stage" not in payload
    assert payload["grading"] == "required"
    public_id = str(payload["review"])
    state = _cycle_payload(state_dir, public_id)
    assert state["selection"] == {
        "requested": "benchmark",
        "effective": "benchmark",
        "reason": "explicit_benchmark",
    }
    assert state["grading"] == {"required": True}


def test_auto_selection_fallback_to_benchmark_persists_reason(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_deslop(monkeypatch)
    _stub_review(monkeypatch)
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")
    config = deepcopy(review.load_config(state_dir))
    del config["orchestrator"]["profiles"]["stable"]["normal"]
    monkeypatch.setattr(review, "load_config", lambda state_dir: config)

    exit_code, payload = _run_review(
        monkeypatch,
        [
            "--mode",
            "normal",
            "--cd",
            str(repo),
            "--base",
            "main",
            "--state-dir",
            str(state_dir),
        ],
    )

    assert exit_code == 0
    assert "selection" not in payload
    assert "stage" not in payload
    assert payload["grading"] == "required"
    state = _cycle_payload(state_dir, str(payload["review"]))
    assert state["selection"] == {
        "requested": "auto",
        "effective": "benchmark",
        "reason": "auto_benchmark_missing_stable",
    }
    assert state["grading"] == {"required": True}


def test_emergency_mode_skips_deslop_and_runs_review(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    review_calls = _stub_review(monkeypatch)

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
    assert "stage" not in payload
    assert set(dict(payload["Action"])) == {"cmd", "alt"}
    assert len(review_calls) == 1
    public_id = str(payload["review"])

    exit_code, clean = _run_review(monkeypatch, ["--id", public_id, "--decision", "clean", "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert "stage" not in clean
    _assert_github_handoff(
        clean["Action"],
        public_id=public_id,
        state_dir=state_dir,
        blocked_by=["full_suite:unknown", "ci:unknown"],
    )
    assert len(review_calls) == 1
    assert _gate_signoff_decisions(state_dir) == []


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
    assert "stage" not in failed
    assert f"--id {public_id}" in str(failed["Action"]["cmd"])
    state = _cycle_payload(state_dir, public_id)
    assert state["deslop"]["status"] == "failed"
    assert state["recovery"]["status"] == "retry-requested"
    assert state["rounds"] == []

    exit_code, retried = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert "stage" not in retried
    assert len(deslop_calls) == 2
    assert _cycle_payload(state_dir, public_id)["deslop"]["status"] == "done"

    exit_code, resumed = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert "stage" not in resumed
    assert len(deslop_calls) == 2
    assert len(review_calls) == 1
