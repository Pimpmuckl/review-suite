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


def _cycle_payload(state_dir: Path, public_id: str) -> dict[str, object]:
    index = json.loads((state_dir / "orchestrator" / "index.json").read_text(encoding="utf-8"))
    cycle_key = index["ids"][public_id]
    return json.loads((state_dir / "orchestrator" / "cycles" / f"{cycle_key}.json").read_text(encoding="utf-8"))


def test_create_resume_and_id_reprint_use_one_pending_action(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
    assert payload["stage"] == "decision-pending"
    assert payload["mode"] == "normal"
    assert payload["selection"] == "stable"
    assert "grading" not in payload
    assert set(dict(payload["Action"])) == {"cmd", "alt"}
    assert f"--id {public_id}" in str(payload["Action"]["cmd"])
    assert "--decision clean" in str(payload["Action"]["cmd"])
    assert "--decision findings" in str(payload["Action"]["alt"])

    exit_code, resumed = _run_review(monkeypatch, args)
    assert exit_code == 0
    assert resumed["review"] == public_id
    assert resumed["Action"] == payload["Action"]
    assert len(list((state_dir / "orchestrator" / "cycles").glob("*.json"))) == 1
    assert len(_cycle_payload(state_dir, public_id)["rounds"]) == 1

    exit_code, by_id = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert by_id["review"] == public_id
    assert by_id["Action"] == payload["Action"]
    assert len(_cycle_payload(state_dir, public_id)["rounds"]) == 1


def test_findings_fix_progression_and_clean_decision(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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

    exit_code, findings = _run_review(monkeypatch, ["--id", public_id, "--decision", "findings", "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert findings["stage"] == "fix-pending"
    assert findings["Action"]["note"] == "Fix valid findings, then rerun this command."
    assert f"--id {public_id}" in str(findings["Action"]["cmd"])
    assert "--decision" not in str(findings["Action"]["cmd"])
    state = _cycle_payload(state_dir, public_id)
    assert state["active_findings"]["round_id"] == "round-1"
    assert len(state["decisions"]) == 1

    exit_code, reprint = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert reprint["stage"] == "fix-pending"
    assert len(_cycle_payload(state_dir, public_id)["rounds"]) == 1

    _commit_file(repo, "app.txt", "fixed\n", "fix findings")
    exit_code, followup = _run_review(monkeypatch, ["--id", public_id, "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert followup["stage"] == "decision-pending"
    assert "--decision clean" in str(followup["Action"]["cmd"])
    state = _cycle_payload(state_dir, public_id)
    assert len(state["rounds"]) == 2
    assert state["rounds"][1]["lane"] == "review-followup"

    exit_code, clean = _run_review(monkeypatch, ["--id", public_id, "--decision", "clean", "--state-dir", str(state_dir)])
    assert exit_code == 0
    assert clean["stage"] == "review-green"
    assert clean["Action"] == {"status": "none"}
    state = _cycle_payload(state_dir, public_id)
    assert state["active_findings"] is None
    assert state["validation"]["review_green"] == "passed"


def test_benchmark_selection_prints_grading_requirement(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
