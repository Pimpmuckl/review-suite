from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import review_deslop


def test_emit_output_only_reports_timeout_without_returncode(capsys) -> None:
    exit_code = review_deslop.emit_output_only(
        tool_name="review-deslop",
        result={"final_message": "", "returncode": None, "timed_out": True},
    )

    assert exit_code == 1
    assert capsys.readouterr().out == "review-deslop run timed out\n"


def test_emit_output_only_treats_uninspectable_success_as_failure(capsys) -> None:
    body = "I couldn't inspect the repository because local process execution is blocked."

    exit_code = review_deslop.emit_output_only(
        tool_name="review-deslop",
        result={"final_message": body, "returncode": 0, "timed_out": False},
    )

    assert exit_code == 1
    assert capsys.readouterr().out == f"{body}\n"


def test_main_routes_base_review_through_codex_review(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(review_deslop, "resolve_repo_root", lambda cd: tmp_path)
    monkeypatch.setattr(review_deslop, "use_unsafe_windows_wsl_fallback", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        review_deslop,
        "lens_model_config",
        lambda name: SimpleNamespace(model="gpt-5.5", reasoning_effort="medium", service_tier=None),
    )

    def fake_run_codex_review(**kwargs):
        captured["run_codex_review"] = kwargs
        return {
            "returncode": 0,
            "stdout": "No findings.",
            "stderr": "",
            "final_message": "No findings.",
            "session_id": "sess-1",
            "elapsed_seconds": 1.0,
            "timed_out": False,
        }

    monkeypatch.setattr(review_deslop, "run_codex_review", fake_run_codex_review)
    monkeypatch.setattr(review_deslop, "run_codex", lambda **kwargs: captured.setdefault("run_codex", kwargs))

    def fake_emit_result(**kwargs):
        captured["emit_result"] = kwargs
        return 0

    monkeypatch.setattr(review_deslop, "emit_result", fake_emit_result)
    monkeypatch.setattr(sys, "argv", ["review_deslop.py", "--cd", str(tmp_path), "--base", "main"])

    assert review_deslop.main() == 0

    assert "run_codex" not in captured
    assert captured["run_codex_review"]["base"] == "main"
    assert captured["run_codex_review"]["commit"] is None
    assert "redundant code" in str(captured["run_codex_review"]["prompt"])


def test_main_precomputes_diff_for_commit_ranges(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(review_deslop, "resolve_repo_root", lambda cd: tmp_path)
    monkeypatch.setattr(review_deslop, "use_unsafe_windows_wsl_fallback", lambda *args, **kwargs: False)
    monkeypatch.setattr(review_deslop, "diff_artifact", lambda review_root, start_ref, end_ref: "diff --git a/x b/x\n")
    monkeypatch.setattr(
        review_deslop,
        "lens_model_config",
        lambda name: SimpleNamespace(model="gpt-5.5", reasoning_effort="medium", service_tier=None),
    )
    monkeypatch.setattr(review_deslop, "run_codex_review", lambda **kwargs: captured.setdefault("run_codex_review", kwargs))

    def fake_run_codex(**kwargs):
        captured["run_codex"] = kwargs
        return {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "final_message": "No findings.",
            "session_id": "sess-1",
            "elapsed_seconds": 1.0,
            "timed_out": False,
        }

    monkeypatch.setattr(review_deslop, "run_codex", fake_run_codex)

    def fake_emit_result(**kwargs):
        captured["emit_result"] = kwargs
        return 0

    monkeypatch.setattr(review_deslop, "emit_result", fake_emit_result)
    monkeypatch.setattr(sys, "argv", ["review_deslop.py", "--commit", "abc123", "def456"])

    assert review_deslop.main() == 0

    assert "run_codex_review" not in captured
    assert "=== BEGIN DIFF ===" in str(captured["run_codex"]["prompt"])
