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
from review_suite_local import write_round


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
        on_round_started = kwargs.get("on_round_started")
        if callable(on_round_started):
            on_round_started(
                {
                    "round_id": round_id,
                    "round_state_dir": "state/orchestrator/review-rounds",
                    "reviewed_head": reviewed_head,
                }
            )
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


def _use_compact_normal_profile(monkeypatch: pytest.MonkeyPatch, state_dir: Path, *, include_deep: bool = False) -> dict[str, object]:
    config = deepcopy(review.load_config(state_dir))
    defaults = config["orchestrator"]["stable_defaults"]
    defaults["normal_discovery_loops"] = 1
    if include_deep:
        defaults["deep_discovery_loops"] = 1
    monkeypatch.setattr(review, "load_config", lambda state_dir: config)
    return config


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
    completion_events: list[str] = []

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
        captured["round_payload"] = dict(round_payload)
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
    monkeypatch.setattr(review_suite_arena, "_print_findings", lambda result: completion_events.append("findings") or False)
    monkeypatch.setattr(review_suite_arena, "record_review_anchor", lambda **kwargs: anchor_calls.append(kwargs))
    monkeypatch.setattr(
        review_suite_arena,
        "launch_review_cost_report_refresh_best_effort",
        lambda **kwargs: completion_events.append("cost") or refresh_calls.append(kwargs) or True,
    )

    result = review_suite_arena.run_orchestrator_review_step(
        lane="review_t1",
        step_name="precision",
        reviewer_count=1,
        model="gpt-5.5",
        reasoning_effort="medium",
        service_tier="flex",
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
    assert dict(captured["round_payload"])["runs"][0]["service_tier"] == "flex"
    assert result["reviewed_head"] == head
    assert result["output_refs"] == ["rollout://alpha"]
    assert banner_calls == [{"task_name": "review 1/2 precision", "round_id": result["round_id"]}]
    assert anchor_calls == []
    assert refresh_calls == [{"state_dir": state_dir, "review_cwd": repo}]
    assert completion_events == ["findings", "cost"]


def test_orchestrator_arena_helper_uses_pr_prompt_for_pr_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")
    _git(repo, "checkout", "-b", "feature/orchestrator-arena-pr")
    head = _commit_file(repo, "app.txt", "feature\n", "feature")
    captured: dict[str, object] = {}
    select_calls: list[dict[str, object]] = []
    anchor_calls: list[dict[str, object]] = []

    def fake_select_pair(**kwargs: object) -> dict[str, object]:
        select_calls.append(dict(kwargs))
        return {
            "round_id": "arena-pr-round-1",
            "task_class": kwargs["task_class"],
            "status": "sampled",
            "runs": [
                {
                    "slot": "alpha",
                    "variant_id": "alpha-model",
                    "model": "gpt-5.5",
                    "reasoning_effort": "xhigh",
                }
            ],
        }

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
        captured["round_payload"] = dict(round_payload)
        return {
            **round_payload,
            "status": "completed",
            "review_scope": dict(review_scope),
            "runs": [
                {
                    "slot": "alpha",
                    "variant_id": "alpha-model",
                    "review_status": "completed",
                    "status_summary": "No findings.",
                    "grade_blocked": False,
                    "grade_block_reason": None,
                    "reviewer_output": "No findings.",
                    "reviewer_output_ref": "rollout://alpha",
                }
            ],
        }

    monkeypatch.setattr(review_suite_arena, "_validate_benchmarked_review_runtime", lambda **kwargs: None)
    monkeypatch.setattr(review_suite_arena, "load_roster", lambda path: {"settings": {}, "variants": []})
    monkeypatch.setattr(review_suite_arena, "load_operational_state", lambda path: {})
    monkeypatch.setattr(review_suite_arena, "select_pair", fake_select_pair)
    monkeypatch.setattr(review_suite_arena, "run_round", fake_run_round)
    monkeypatch.setattr(review_suite_arena, "_print_round_banner", lambda **kwargs: None)
    monkeypatch.setattr(review_suite_arena, "_print_findings", lambda result: False)
    monkeypatch.setattr(review_suite_arena, "record_review_anchor", lambda **kwargs: anchor_calls.append(kwargs))
    monkeypatch.setattr(review_suite_arena, "launch_review_cost_report_refresh_best_effort", lambda **kwargs: True)

    result = review_suite_arena.run_orchestrated_arena_round(
        lane="review_t3",
        task_class="pr_review",
        step_name="arena-pr-review",
        review_cwd=repo,
        state_dir=state_dir,
        sqlite_path=tmp_path / "state.sqlite",
        review_scope={"base": "main"},
        task_id="feature/orchestrator-arena-pr",
        allow_dirty=False,
        progress_interval_seconds=1,
        allow_unsafe_windows_wsl_fallback=False,
        step_position=3,
        step_total=4,
    )

    prompt = str(captured["prompt"])
    assert "Review this PR-ready branch diff" in prompt
    assert "Review this implementation slice" not in prompt
    assert dict(captured["review_scope"])["manual_prompt_mode"] is True
    assert dict(captured["round_payload"])["task_class"] == "pr_review"
    assert dict(captured["round_payload"])["task_id_hint"] == "feature/orchestrator-arena-pr"
    assert "Review this PR-ready branch diff" in str(dict(captured["round_payload"])["requested_prompt"])
    assert dict(captured["round_payload"])["review_scope"] == dict(captured["review_scope"])
    assert dict(captured["round_payload"])["review_cwd"] == str(repo)
    assert dict(captured["round_payload"])["allow_dirty"] is False
    assert dict(captured["round_payload"])["progress_interval_seconds"] == 1
    assert select_calls[0]["task_class"] == "pr_review"
    assert result["lane"] == "review_t3"
    assert result["reviewed_head"] == head
    assert result["arena_round"] is True
    assert result["grading_required"] is True
    assert result["needs_grade"] is True
    assert result["graded"] is False
    assert anchor_calls == []


def test_decision_action_surfaces_arena_grade_command(tmp_path: Path) -> None:
    state = {
        "public_id": "rvw_example",
        "stage": "decision-pending",
        "pending_action": {"kind": "decision", "round_id": "arena-round-1", "lane": "review_t3"},
        "identity": {"branch": "feature/arena"},
        "deslop": {"tracked": False, "status": "closed"},
        "rounds": [
            {
                "round_id": "arena-round-1",
                "lane": "review_t3",
                "grading_required": True,
                "arena_round": True,
                "status": "completed",
                "task_id_hint": "feature/arena",
            }
        ],
    }

    action = review._action_payload(state, state_dir=tmp_path / "state")

    assert action is not None
    assert "review_suite_arena.py grade" in str(action["cmd"])
    assert "--round-id arena-round-1" in str(action["cmd"])
    assert "--task-id feature/arena" in str(action["cmd"])
    assert "--state-dir" in str(action["cmd"])
    assert "--id rvw_example" in str(action["next"])
    assert "alt" not in action


def test_arena_recovery_action_surfaces_reroll_and_dismiss(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state = {
        "public_id": "rvw_example",
        "stage": "retry-requested",
        "pending_action": {
            "kind": "arena-blocked",
            "round_id": "arena-round-1",
            "lane": "review_t3",
            "step_index": 0,
            "step": "arena-discovery",
        },
        "identity": {"cwd": str(repo), "base": "main", "branch": "feature/arena"},
        "deslop": {"tracked": False, "status": "closed"},
        "rounds": [
            {
                "round_id": "arena-round-1",
                "lane": "review_t3",
                "grading_required": True,
                "arena_round": True,
                "status": "completed",
                "runs": [{"slot": "alpha", "blocked": True}],
            }
        ],
    }

    action = review._action_payload(state, state_dir=tmp_path / "state")

    assert action is not None
    assert "review_suite_arena.py reroll-slot" in str(action["cmd"])
    assert "--round-id arena-round-1" in str(action["cmd"])
    assert "--slot alpha" in str(action["cmd"])
    assert "review_suite_arena.py dismiss-round" in str(action["dismiss"])
    assert "--id rvw_example" in str(action["next"])


def test_arena_recovery_action_surfaces_run_for_sampled_replacement(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state = {
        "public_id": "rvw_example",
        "stage": "retry-requested",
        "pending_action": {
            "kind": "arena-blocked",
            "round_id": "arena-round-2",
            "lane": "review_t3",
            "step_index": 0,
            "step": "arena-discovery",
        },
        "identity": {"cwd": str(repo), "base": "main", "branch": "feature/arena"},
        "deslop": {"tracked": False, "status": "closed"},
        "rounds": [
            {
                "round_id": "arena-round-2",
                "lane": "review_t3",
                "grading_required": True,
                "arena_round": True,
                "status": "sampled",
                "runs": [{"slot": "alpha"}],
            }
        ],
    }

    action = review._action_payload(state, state_dir=tmp_path / "state")

    assert action is not None
    assert "review_suite_arena.py run-round" in str(action["cmd"])
    assert "--round-id arena-round-2" in str(action["cmd"])
    assert "review_suite_arena.py dismiss-round" in str(action["dismiss"])
    assert "--id rvw_example" in str(action["next"])


def test_decision_rejects_ungraded_arena_round(tmp_path: Path) -> None:
    state = {
        "public_id": "rvw_example",
        "stage": "decision-pending",
        "pending_action": {"kind": "decision", "round_id": "arena-round-1", "lane": "review_t3"},
        "identity": {"branch": "feature/arena"},
        "review_progress": {"next_step_index": 1, "completed_steps": []},
        "rounds": [
            {
                "round_id": "arena-round-1",
                "lane": "review_t3",
                "grading_required": True,
                "arena_round": True,
                "status": "completed",
                "reviewed_head": "head-1",
            }
        ],
    }

    with pytest.raises(ValueError, match="grade the arena round"):
        review._apply_decision(state, "clean", state_dir=tmp_path / "state")


def test_benchmark_grading_required_round_does_not_use_arena_grade_gate(tmp_path: Path) -> None:
    state = {
        "public_id": "rvw_example",
        "stage": "decision-pending",
        "pending_action": {"kind": "decision", "round_id": "benchmark-round-1", "lane": "review_t1"},
        "identity": {"branch": "feature/benchmark"},
        "review_progress": {"next_step_index": 1, "completed_steps": []},
        "review_plan": {"steps": [{"name": "benchmark", "count": 1, "model": "gpt-5.5", "reasoning_effort": "medium"}]},
        "rounds": [
            {
                "round_id": "benchmark-round-1",
                "lane": "review_t1",
                "grading_required": True,
                "status": "completed",
                "reviewed_head": "head-1",
            }
        ],
    }

    action = review._action_payload(state, state_dir=tmp_path / "state")
    clean = review._apply_decision(state, "clean", state_dir=tmp_path / "state")

    assert action is not None
    assert "--decision clean" in str(action["cmd"])
    assert "review_suite_arena.py grade" not in str(action)
    assert clean["stage"] == "review-green"


def test_create_resume_and_id_reprint_use_one_pending_action(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    deslop_calls = _stub_deslop(monkeypatch)
    review_calls = _stub_review(monkeypatch, "phase_review-round-1", "phase_review-round-2")
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _use_compact_normal_profile(monkeypatch, state_dir)
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
    assert set(dict(payload["Action"])) == {"cmd", "deslop_done"}
    assert f"--id {public_id}" in str(payload["Action"]["cmd"])
    assert "--decision" not in str(payload["Action"]["cmd"])
    assert "--deslop-done" in str(payload["Action"]["deslop_done"])
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
    assert set(dict(resumed["Action"])) == {"cmd", "alt", "deslop_done"}
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
    assert set(dict(first_clean["Action"])) == {"cmd", "deslop_done"}
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


def test_id_show_findings_reads_orchestrator_round_payload_without_running(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _stub_deslop(monkeypatch)
    review_calls = _stub_review(monkeypatch, "phase_review-round-1")
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")
    _git(repo, "checkout", "-b", "feature/show-findings")
    _commit_file(repo, "app.txt", "feature\n", "feature")

    args = ["--mode", "brief", "--cd", str(repo), "--base", "main", "--state-dir", str(state_dir)]
    _, created = _run_review(monkeypatch, args)
    public_id = str(created["review"])
    _run_review(monkeypatch, args)
    state = _cycle_payload(state_dir, public_id)
    round_id = str(state["rounds"][0]["round_id"])
    round_state_dir = state_dir / "orchestrator" / "review-rounds"
    state["rounds"][0]["round_state_dir"] = str(round_state_dir)
    state["rounds"][0]["status"] = "selected-round-status"
    state["rounds"].append(
        {
            "round_id": "empty-later-round",
            "lane": "review_t2",
            "status": "decision-pending",
            "round_state_dir": str(round_state_dir),
            "runs": [],
        }
    )
    state["pending_action"] = {"kind": "decision", "round_id": "empty-later-round", "lane": "review_t2"}
    _write_cycle_payload(state_dir, public_id, state)
    write_round(
        round_state_dir,
        {
            "round_id": round_id,
            "task_class": "phase_review",
            "public_task": "review_t1",
            "review_cwd": str(repo),
            "runs": [
                {
                    "slot": "alpha",
                    "review_status": "completed",
                    "reviewer_output": "Alpha recovered finding",
                    "reviewer_output_ref": "rollout://phase_review-round-1/alpha",
                }
            ],
        },
    )
    write_round(
        round_state_dir,
        {
            "round_id": "empty-later-round",
            "task_class": "phase_gate",
            "status": "completed",
            "runs": [],
        },
    )
    before_calls = len(review_calls)
    monkeypatch.setattr(review, "emit_toon", lambda payload: (_ for _ in ()).throw(AssertionError("should not emit status")))
    monkeypatch.setattr(
        sys,
        "argv",
        ["review.py", "--id", public_id, "--show-findings", "--state-dir", str(state_dir)],
    )

    exit_code = review.main()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert f"review: {public_id}" in captured.out
    assert f"round_id: {round_id}" in captured.out
    assert "status: selected-round-status" in captured.out
    assert "status: decision-pending" not in captured.out
    assert "Alpha recovered finding" in captured.out
    assert len(review_calls) == before_calls


def test_id_collects_running_round_without_spawning_duplicate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_deslop(monkeypatch)
    review_calls = _stub_review(monkeypatch, "phase_review-round-1", "phase_review-round-duplicate")
    resume_calls: list[dict[str, object]] = []
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")
    _git(repo, "checkout", "-b", "feature/running-review")
    _commit_file(repo, "app.txt", "feature\n", "feature")

    args = ["--mode", "normal", "--cd", str(repo), "--base", "main", "--state-dir", str(state_dir)]
    _, created = _run_review(monkeypatch, args)
    public_id = str(created["review"])
    _run_review(monkeypatch, args)

    state = _cycle_payload(state_dir, public_id)
    round_id = str(state["rounds"][0]["round_id"])
    state["stage"] = "running"
    state["pending_action"] = {
        "kind": "collect-review-step",
        "round_id": round_id,
        "lane": "review_t1",
        "step_index": 0,
        "step": "broad-discovery",
        "round_state_dir": "state/orchestrator/review-rounds",
    }
    state["rounds"][0]["status"] = "running"
    _write_cycle_payload(state_dir, public_id, state)

    def fake_resume(**kwargs: object) -> dict[str, object]:
        resume_calls.append(dict(kwargs))
        return {
            "round_id": round_id,
            "lane": "review_t1",
            "kind": "review",
            "status": "completed",
            "blocked": False,
            "reviewed_head": state["rounds"][0]["reviewed_head"],
            "output_refs": ["rollout://phase_review-round-1/resumed"],
            "runs": [
                {
                    "slot": "alpha",
                    "status": "completed",
                    "summary": "No findings.",
                    "ref": "rollout://phase_review-round-1/resumed",
                    "blocked": False,
                    "block": None,
                }
            ],
            "round_state_dir": "state/orchestrator/review-rounds",
        }

    monkeypatch.setattr(orchestrator_runner, "resume_review_step", fake_resume)

    exit_code, payload = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])

    assert exit_code == 0
    assert payload["review"] == public_id
    assert "--decision clean" in str(payload["Action"]["cmd"])
    assert len(review_calls) == 1
    assert len(resume_calls) == 1
    assert resume_calls[0]["round_id"] == round_id
    saved = _cycle_payload(state_dir, public_id)
    assert saved["stage"] == "decision-pending"
    assert saved["pending_action"]["kind"] == "decision"
    assert [item["round_id"] for item in saved["rounds"]] == [round_id]
    assert saved["rounds"][0]["output_refs"] == ["rollout://phase_review-round-1/resumed"]


def test_restart_mode_supersedes_cycle_and_starts_fresh_deep_ladder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deslop_calls = _stub_deslop(monkeypatch)
    review_calls = _stub_review(monkeypatch, "normal-round-1", "deep-round-1")
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _use_compact_normal_profile(monkeypatch, state_dir, include_deep=True)
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
        "precision-signoff",
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
    assert review_calls[1]["step_total"] == 4


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


def test_deslop_done_closes_tracked_sidecar_without_rerunning_deslop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deslop_calls = _stub_deslop(monkeypatch)
    review_calls = _stub_review(monkeypatch, "phase_review-round-1")
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")

    _, created = _run_review(
        monkeypatch,
        ["--mode", "normal", "--cd", str(repo), "--base", "main", "--state-dir", str(state_dir)],
    )
    public_id = str(created["review"])

    assert "--deslop-done" in str(created["Action"]["deslop_done"])
    assert len(deslop_calls) == 1

    exit_code, closed = _run_review(
        monkeypatch,
        ["--id", public_id, "--deslop-done", "--state-dir", str(state_dir)],
    )

    assert exit_code == 0
    assert closed["review"] == public_id
    assert "deslop_done" not in dict(closed["Action"])
    state = _cycle_payload(state_dir, public_id)
    assert state["deslop"]["tracked"] is False
    assert state["deslop"]["status"] == "closed"

    exit_code, resumed = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])

    assert exit_code == 0
    assert "--decision clean" in str(resumed["Action"]["cmd"])
    assert "deslop_done" not in dict(resumed["Action"])
    assert len(deslop_calls) == 1
    assert len(review_calls) == 1


def test_deslop_done_is_primary_action_when_no_other_action_remains() -> None:
    state = {"deslop": {"tracked": True, "status": "done"}}

    action = review._with_deslop_done_action(state, None, "rvw_example")

    assert action == {"cmd": review._review_command("rvw_example", "--deslop-done")}


def test_deslop_done_requires_id_and_rejects_other_actions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    errors: list[str] = []
    monkeypatch.setattr(review, "emit_error", lambda message, **kwargs: errors.append(str(message)) or 2)
    monkeypatch.setattr(sys, "argv", ["review.py", "--deslop-done", "--state-dir", str(tmp_path / "state")])

    assert review.main() == 2
    assert errors[-1] == "--deslop-done requires --id"

    monkeypatch.setattr(
        sys,
        "argv",
        ["review.py", "--id", "rvw_example", "--deslop-done", "--show-findings", "--state-dir", str(tmp_path / "state")],
    )

    assert review.main() == 2
    assert "--deslop-done cannot be combined" in errors[-1]


def test_deslop_done_is_noop_for_emergency_cycle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fail_deslop(*, command: list[str], cwd: Path) -> subprocess.CompletedProcess:
        raise AssertionError("emergency mode must not run deslop")

    monkeypatch.setattr(orchestrator_runner, "run_deslop_subprocess", fail_deslop)
    _stub_review(monkeypatch, "phase_review-round-1")
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")

    _, created = _run_review(
        monkeypatch,
        ["--mode", "emergency", "--cd", str(repo), "--base", "main", "--state-dir", str(state_dir)],
    )
    public_id = str(created["review"])

    exit_code, closed = _run_review(
        monkeypatch,
        ["--id", public_id, "--deslop-done", "--state-dir", str(state_dir)],
    )

    assert exit_code == 0
    assert "--decision clean" in str(closed["Action"]["cmd"])
    state = _cycle_payload(state_dir, public_id)
    assert state["deslop"] == {"tracked": False, "status": "skipped-emergency"}


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


def test_github_result_findings_reenters_existing_cycle_for_final_signoff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub_deslop(monkeypatch)
    review_calls = _stub_review(monkeypatch, "signoff-round-1", "signoff-round-2")
    followup_calls = _stub_followup(monkeypatch, "github-followup-1")
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")
    _git(repo, "checkout", "-b", "feature/github-findings")
    _commit_file(repo, "app.txt", "feature\n", "feature")

    _, opened = _run_review(
        monkeypatch,
        ["--mode", "emergency", "--cd", str(repo), "--base", "main", "--state-dir", str(state_dir)],
    )
    public_id = str(opened["review"])
    _, green = _run_review(monkeypatch, ["--id", public_id, "--decision", "clean", "--state-dir", str(state_dir)])
    _assert_github_handoff(
        green["Action"],
        public_id=public_id,
        state_dir=state_dir,
        blocked_by=["full_suite:unknown", "ci:unknown"],
    )

    exit_code, github_findings = _run_review(
        monkeypatch,
        [
            "--id",
            public_id,
            "--github-result",
            "findings",
            "--github-note",
            "GitHub found a stale replay bug.",
            "--state-dir",
            str(state_dir),
        ],
    )

    assert exit_code == 0
    assert github_findings["github_review"] == "findings"
    assert github_findings["Action"]["note"] == "Fix valid findings, then rerun this command."
    state = _cycle_payload(state_dir, public_id)
    assert state["active_findings"]["lane"] == "review-github"
    assert state["active_findings"]["profile_round_id"] == "signoff-round-1"
    assert state["validation"]["review_green"] == "unknown"

    _commit_file(repo, "app.txt", "feature\nfixed\n", "fix github finding")
    _, signoff = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    assert "--decision clean" in str(signoff["Action"]["cmd"])
    assert len(followup_calls) == 0
    assert len(review_calls) == 2
    assert review_calls[1]["step_name"] == "urgent-signoff"
    assert review_calls[1]["step_position"] == 1
    assert review_calls[1]["step_total"] == 1
    state = _cycle_payload(state_dir, public_id)
    assert state["pending_action"]["round_id"] == "signoff-round-2"
    assert state["review_progress"]["completed_steps"] == []

    _, final_clean = _run_review(monkeypatch, ["--id", public_id, "--decision", "clean", "--state-dir", str(state_dir)])
    _assert_github_handoff(final_clean["Action"], public_id=public_id, state_dir=state_dir, blocked_by=["full_suite:unknown", "ci:unknown"])
    assert final_clean["Action"]["note"] == "GitHub findings were fixed and locally signed off; request GitHub review again."


def test_github_result_clean_and_waived_are_terminal_for_existing_cycle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub_deslop(monkeypatch)
    _stub_review(monkeypatch)
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")

    _, opened = _run_review(
        monkeypatch,
        ["--mode", "emergency", "--cd", str(repo), "--base", "main", "--state-dir", str(state_dir)],
    )
    public_id = str(opened["review"])
    _run_review(monkeypatch, ["--id", public_id, "--decision", "clean", "--state-dir", str(state_dir)])

    exit_code, clean = _run_review(monkeypatch, ["--id", public_id, "--github-result", "clean", "--state-dir", str(state_dir)])

    assert exit_code == 0
    assert clean["github_review"] == "clean"
    assert clean["Action"]["blocked_by"] == ["full_suite:unknown", "ci:unknown"]
    assert "--full-suite passed --ci passed" in str(clean["Action"]["cmd"])
    assert "--full-suite waived --ci waived" in str(clean["Action"]["alt"])

    _run_review(monkeypatch, ["--id", public_id, "--full-suite", "classified", "--ci", "classified", "--state-dir", str(state_dir)])
    exit_code, clean = _run_review(monkeypatch, ["--id", public_id, "--github-result", "clean", "--state-dir", str(state_dir)])

    assert exit_code == 0
    assert clean["github_review"] == "clean"
    assert "Action" not in clean

    state = _cycle_payload(state_dir, public_id)
    state["github_review"] = {"status": "unknown"}
    state["validation"]["full_suite"] = "unknown"
    state["validation"]["ci"] = "unknown"
    _write_cycle_payload(state_dir, public_id, state)
    errors: list[tuple[str, dict[str, object]]] = []

    def fake_error(message: str, **kwargs: object) -> int:
        errors.append((message, dict(kwargs)))
        return 2

    monkeypatch.setattr(review, "emit_error", fake_error)
    monkeypatch.setattr(sys, "argv", ["review.py", "--id", public_id, "--github-result", "waived", "--state-dir", str(state_dir)])

    assert review.main() == 2
    assert errors[0][0] == "--github-note is required when --github-result waived"

    monkeypatch.setattr(review, "emit_toon", lambda payload: None)
    exit_code, waived = _run_review(
        monkeypatch,
        [
            "--id",
            public_id,
            "--github-result",
            "waived",
            "--github-note",
            "parent agent approved no-github after gh timeout",
            "--state-dir",
            str(state_dir),
        ],
    )

    assert exit_code == 0
    assert waived["github_review"] == "waived"
    assert waived["Action"]["blocked_by"] == ["full_suite:unknown", "ci:unknown"]

    _commit_file(repo, "app.txt", "base\nnew work\n", "new work after github waiver")
    _, stale = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    assert stale["github_review"] == "waived"
    assert "--github-review" in str(stale["Action"]["cmd"])


def test_github_result_findings_does_not_auto_start_followup_when_fix_already_committed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub_deslop(monkeypatch)
    _stub_review(monkeypatch)
    followup_calls = _stub_followup(monkeypatch)
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")
    _git(repo, "checkout", "-b", "feature/github-result-boundary")
    _commit_file(repo, "app.txt", "feature\n", "feature")

    _, opened = _run_review(
        monkeypatch,
        ["--mode", "emergency", "--cd", str(repo), "--base", "main", "--state-dir", str(state_dir)],
    )
    public_id = str(opened["review"])
    _run_review(monkeypatch, ["--id", public_id, "--decision", "clean", "--state-dir", str(state_dir)])
    _commit_file(repo, "app.txt", "feature\nfix already committed\n", "fix before recording github result")

    exit_code, findings = _run_review(
        monkeypatch,
        ["--id", public_id, "--github-result", "findings", "--state-dir", str(state_dir)],
    )

    assert exit_code == 0
    assert findings["Action"]["note"] == "Fix valid findings, then rerun this command."
    assert len(followup_calls) == 0
    state = _cycle_payload(state_dir, public_id)
    assert state["stage"] == "fix-pending"
    assert state["active_findings"]["reviewed_head"] == state["review_heads"]["last_reviewed_head"]


def test_findings_fix_progression_and_clean_decision(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_deslop(monkeypatch)
    review_calls = _stub_review(monkeypatch, "phase_review-round-1", "phase_review-round-2")
    followup_calls = _stub_followup(monkeypatch, "followup-round-1")
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _use_compact_normal_profile(monkeypatch, state_dir)
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
    exit_code, second_step = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert "stage" not in second_step
    assert "--decision clean" in str(second_step["Action"]["cmd"])
    assert len(followup_calls) == 0
    assert len(review_calls) == 2
    assert review_calls[1]["step_name"] == "precision-signoff"
    assert review_calls[1]["step_position"] == 2
    assert review_calls[1]["step_total"] == 2
    state = _cycle_payload(state_dir, public_id)
    assert len(state["rounds"]) == 2
    assert state["rounds"][1]["lane"] == "review_t1"
    assert state["rounds"][1]["round_id"] == "phase_review-round-2"

    exit_code, clean = _run_review(monkeypatch, ["--id", public_id, "--decision", "clean", "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert "stage" not in clean
    _assert_github_handoff(
        clean["Action"],
        public_id=public_id,
        state_dir=state_dir,
        blocked_by=["full_suite:unknown", "ci:unknown"],
    )
    state = _cycle_payload(state_dir, public_id)
    assert state["active_findings"] is None
    assert state["validation"]["review_green"] == "passed"
    assert state["pending_action"] is None
    assert state["review_progress"]["completed_steps"] == [
        {
            "index": 0,
            "name": "broad-discovery",
            "round_id": "phase_review-round-1",
            "lane": "review_t1",
            "reviewed_head": state["rounds"][0]["reviewed_head"],
        },
        {
            "index": 1,
            "name": "precision-signoff",
            "round_id": "phase_review-round-2",
            "lane": "review_t1",
            "reviewed_head": state["rounds"][1]["reviewed_head"],
        },
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
    _use_compact_normal_profile(monkeypatch, state_dir)
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
    _use_compact_normal_profile(monkeypatch, state_dir)
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
    exit_code, signoff_rerun = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])

    assert exit_code == 0
    assert "stage" not in signoff_rerun
    assert "--decision clean" in str(signoff_rerun["Action"]["cmd"])
    assert len(review_calls) == 3
    assert review_calls[2]["step_name"] == "precision-signoff"
    assert len(followup_calls) == 0
    state = _cycle_payload(state_dir, public_id)
    assert state["pending_action"]["round_id"] == "phase_review-round-3"
    assert [item["round_id"] for item in state["review_progress"]["completed_steps"]] == ["phase_review-round-1"]

    exit_code, green = _run_review(monkeypatch, ["--id", public_id, "--decision", "clean", "--state-dir", str(state_dir)])

    assert exit_code == 0
    assert "stage" not in green
    state = _cycle_payload(state_dir, public_id)
    assert [item["round_id"] for item in state["review_progress"]["completed_steps"]] == [
        "phase_review-round-1",
        "phase_review-round-3",
    ]


def test_clean_followup_note_does_not_leak_to_later_review_steps(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_deslop(monkeypatch)
    _stub_review(monkeypatch, "phase_review-round-1", "phase_review-round-2", "phase_review-round-3")
    _stub_followup(monkeypatch, "followup-round-1")
    config = deepcopy(review.load_config(tmp_path / "state"))
    config["orchestrator"]["profiles"]["stable"]["deep"]["steps"] = [
        {"name": "precision-signoff", "count": 1, "model_ref": "signoff_deep_model", "rerun_on_findings": True},
        {"name": "final-sweep", "count": 1, "model_ref": "signoff_deep_model"},
    ]
    monkeypatch.setattr(review, "load_config", lambda state_dir: config)
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")
    _git(repo, "checkout", "-b", "feature/signoff-note-stale")
    _commit_file(repo, "app.txt", "feature\n", "feature")

    _, created = _run_review(
        monkeypatch,
        ["--mode", "deep", "--cd", str(repo), "--base", "main", "--state-dir", str(state_dir)],
    )
    public_id = str(created["review"])
    _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    _run_review(monkeypatch, ["--id", public_id, "--decision", "findings", "--state-dir", str(state_dir)])
    _commit_file(repo, "app.txt", "fixed\n", "fix signoff finding")
    _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])

    _, rerun_ready = _run_review(monkeypatch, ["--id", public_id, "--decision", "clean", "--state-dir", str(state_dir)])

    assert rerun_ready["Action"]["note"] == (
        "Clean follow-up is not final signoff; run review step 1/2 precision-signoff "
        "before treating the review as green."
    )

    _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    _, final_step_ready = _run_review(monkeypatch, ["--id", public_id, "--decision", "clean", "--state-dir", str(state_dir)])

    assert set(dict(final_step_ready["Action"])) == {"cmd", "deslop_done"}
    state = _cycle_payload(state_dir, public_id)
    assert state["pending_action"] == {"kind": "run-review-step", "step_index": 1, "step": "final-sweep"}


def test_gate_findings_flow_reruns_same_gate_without_followup_on_normal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_deslop(monkeypatch)
    _stub_review(monkeypatch, "phase_review-round-1", "phase_review-round-2")
    followup_calls = _stub_followup(monkeypatch, "followup-round-1")
    gate_calls = _stub_gate(monkeypatch, "phase_gate-round-1", "phase_gate-round-2")
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    config = deepcopy(review.load_config(state_dir))
    config["orchestrator"]["stable_defaults"]["normal_discovery_loops"] = 1
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
    exit_code, gate_rerun = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert "stage" not in gate_rerun
    assert "--decision clean" in str(gate_rerun["Action"]["cmd"])
    assert len(followup_calls) == 0
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
        ["--mode", "deep", "--cd", str(repo), "--base", "main", "--state-dir", str(state_dir)],
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


def test_emergency_mode_stops_after_two_local_review_rounds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    review_calls = _stub_review(monkeypatch, "urgent-1", "urgent-2", "urgent-3")
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    _init_repo(repo)
    _commit_file(repo, "app.txt", "base\n", "base")

    _, opened = _run_review(
        monkeypatch,
        ["--mode", "emergency", "--cd", str(repo), "--base", "main", "--state-dir", str(state_dir)],
    )
    public_id = str(opened["review"])
    _run_review(monkeypatch, ["--id", public_id, "--decision", "findings", "--state-dir", str(state_dir)])
    _commit_file(repo, "app.txt", "base\nfix one\n", "fix first emergency finding")
    _, second_round = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    assert "--decision findings" in str(second_round["Action"]["alt"])
    assert len(review_calls) == 2

    _run_review(monkeypatch, ["--id", public_id, "--decision", "findings", "--state-dir", str(state_dir)])
    _commit_file(repo, "app.txt", "base\nfix one\nfix two\n", "fix second emergency finding")
    _, exhausted = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])

    assert len(review_calls) == 2
    assert "reached its 2 round review budget" in str(exhausted["Action"]["note"])
    state = _cycle_payload(state_dir, public_id)
    assert state["stage"] == "fix-pending"
    assert state["validation"]["review_green"] == "failed"
    assert state["pending_action"]["kind"] == "review-round-budget-exhausted"
    assert [item["round_id"] for item in state["rounds"]] == ["urgent-1", "urgent-2"]
    fresh_token = review._fresh_review_token(state)
    assert "--mode emergency" in str(exhausted["Action"]["cmd"])
    assert "--fresh-token" in str(exhausted["Action"]["cmd"])
    assert fresh_token in str(exhausted["Action"]["cmd"])

    _, fresh = _run_review(
        monkeypatch,
        [
            "--mode",
            "emergency",
            "--cd",
            str(repo),
            "--base",
            "main",
            "--state-dir",
            str(state_dir),
            "--fresh-token",
            fresh_token,
        ],
    )
    assert str(fresh["review"]) != public_id
    assert len(review_calls) == 3
    fresh_state = _cycle_payload(state_dir, str(fresh["review"]))
    assert fresh_state["fresh"]["token"] == fresh_token
    assert "restart" not in fresh_state
    assert [item["round_id"] for item in fresh_state["rounds"]] == ["urgent-3"]


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
