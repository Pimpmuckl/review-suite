from __future__ import annotations

import subprocess
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
    body = (
        "I couldn't inspect the repository because local process execution is blocked."
    )

    exit_code = review_deslop.emit_output_only(
        tool_name="review-deslop",
        result={"final_message": body, "returncode": 0, "timed_out": False},
    )

    assert exit_code == 1
    assert capsys.readouterr().out == f"{body}\n"


def test_emit_output_only_treats_clean_text_as_success(capsys) -> None:
    body = "No findings."

    exit_code = review_deslop.emit_output_only(
        tool_name="review-deslop",
        result={"final_message": body, "returncode": 1, "timed_out": False},
    )

    assert exit_code == 0
    assert capsys.readouterr().out == f"{body}\n"


def test_clean_detection_rejects_clean_phrase_with_findings() -> None:
    body = "No findings.\n\nReview result: findings"

    assert (
        review_deslop._deslop_output_clean(
            {"final_message": body, "stdout": "", "stderr": ""}
        )
        is False
    )


def test_static_cleanup_parser_keeps_high_confidence_touched_items() -> None:
    output = "\n".join(
        [
            "app.py:1: unused import 'os' (90% confidence)",
            "app.py:9: unreachable code after 'return' (100% confidence)",
            "app.py:12: unused function 'helper' (60% confidence, 2 lines)",
            "app.py:20: unused import 'json' (90% confidence)",
            "other.py:2: unused import 'sys' (90% confidence)",
        ]
    )

    suggestions = review_deslop._parse_static_cleanup_output(
        output, {"app.py": {1, 9, 12}}
    )

    assert suggestions == [
        review_deslop.StaticCleanupSuggestion("app.py", 1, "unused import 'os'", 90),
        review_deslop.StaticCleanupSuggestion(
            "app.py", 9, "unreachable code after 'return'", 100
        ),
    ]


def test_normalize_repo_path_preserves_dot_prefixed_paths() -> None:
    assert (
        review_deslop._normalize_repo_path("./.github/scripts/check.py")
        == ".github/scripts/check.py"
    )
    assert (
        review_deslop._normalize_repo_path(".github\\scripts\\check.py")
        == ".github/scripts/check.py"
    )


def test_static_cleanup_scan_skips_single_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        review_deslop.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("commit mode must not scan the worktree")
        ),
    )

    assert (
        review_deslop._start_static_cleanup_scan(
            review_root=tmp_path, base=None, commit="abc123", commit_end=None
        )
        is None
    )


def test_static_cleanup_scan_uses_two_commit_range(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    diff_ranges: list[str] = []
    monkeypatch.setattr(
        review_deslop,
        "_changed_python_lines",
        lambda **kwargs: diff_ranges.append(kwargs["diff_range"]) or {},
    )

    assert (
        review_deslop._start_static_cleanup_scan(
            review_root=tmp_path, base=None, commit="abc123", commit_end="def456"
        )
        is None
    )
    assert diff_ranges == ["abc123..def456"]


def test_static_cleanup_scan_skip_without_changed_python(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    diff_ranges: list[str] = []
    monkeypatch.setattr(
        review_deslop,
        "_changed_python_lines",
        lambda **kwargs: diff_ranges.append(kwargs["diff_range"]) or {},
    )
    monkeypatch.setattr(
        review_deslop.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("non-python diffs must not scan")
        ),
    )

    assert (
        review_deslop._start_static_cleanup_scan(
            review_root=tmp_path, base="origin/main", commit=None, commit_end=None
        )
        is None
    )
    assert diff_ranges == ["origin/main...HEAD"]


def test_static_cleanup_scan_starts_with_exact_tracked_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        review_deslop, "_ensure_vulture_command", lambda: ["python", "-m", "vulture"]
    )
    monkeypatch.setattr(
        review_deslop, "_changed_python_lines", lambda **kwargs: {"pkg/app.py": {1}}
    )

    class FakeProcess:
        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["cwd"] = kwargs["cwd"]
        captured["stdout"] = kwargs["stdout"]
        captured["stderr"] = kwargs["stderr"]
        kwargs["stdout"].write("pkg/app.py:1: unused import 'os' (90% confidence)\n")
        kwargs["stdout"].flush()
        return FakeProcess()

    monkeypatch.setattr(review_deslop.subprocess, "Popen", fake_popen)

    scan = review_deslop._start_static_cleanup_scan(
        review_root=tmp_path, base="origin/main", commit=None, commit_end=None
    )

    assert scan is not None
    assert captured["cwd"] == tmp_path
    assert captured["command"] == [
        "python",
        "-m",
        "vulture",
        "pkg/app.py",
        "--min-confidence",
        "90",
    ]
    assert captured["stdout"] != subprocess.PIPE
    assert captured["stderr"] != subprocess.PIPE
    assert review_deslop._collect_static_cleanup_scan(scan) == [
        review_deslop.StaticCleanupSuggestion("pkg/app.py", 1, "unused import 'os'", 90)
    ]
    assert not scan.stdout_path.exists()
    assert not scan.stderr_path.exists()


def test_static_cleanup_collect_does_not_wait_for_running_scan(tmp_path: Path) -> None:
    class FakeProcess:
        stopped = False

        def poll(self):
            return None

        def terminate(self):
            self.stopped = True

        def wait(self, timeout):
            return 0

        def kill(self):
            raise AssertionError("terminated process should not be killed")

    process = FakeProcess()
    stdout_path = tmp_path / "stdout.txt"
    stderr_path = tmp_path / "stderr.txt"
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")

    suggestions = review_deslop._collect_static_cleanup_scan(
        review_deslop.StaticCleanupScan(
            process=process,
            changed_lines={"app.py": {1}},
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
    )

    assert suggestions == []
    assert process.stopped is True
    assert not stdout_path.exists()
    assert not stderr_path.exists()


def test_static_cleanup_output_prefixes_successful_deslop_result() -> None:
    result = {
        "returncode": 0,
        "stdout": "",
        "stderr": "",
        "final_message": "No findings.",
        "session_id": "sess-1",
        "elapsed_seconds": 1.0,
        "timed_out": False,
    }
    suggestions = [
        review_deslop.StaticCleanupSuggestion("app.py", 1, "unused import 'os'", 90)
    ]

    updated = review_deslop._with_static_cleanup_output(result, suggestions)

    assert (
        updated["final_message"]
        == "Static cleanup suggestions:\n- Low - app.py:1 - unused import 'os'. Fix: Remove the unused import.\n\nDeslop Results:\nNo reviewer findings.\nReview decision: findings"
    )


def test_main_uses_generic_read_only_deslop_review(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(review_deslop, "resolve_repo_root", lambda cd: tmp_path)
    monkeypatch.setattr(
        review_deslop, "ensure_clean_git_worktree", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        review_deslop,
        "effective_base_ref",
        lambda review_root, base: {"base": "origin/main", "requested_base": base},
    )
    order: list[str] = []
    monkeypatch.setattr(
        review_deslop,
        "_start_static_cleanup_scan",
        lambda **kwargs: order.append("start") or "scan",
    )
    monkeypatch.setattr(
        review_deslop,
        "_collect_static_cleanup_scan",
        lambda scan: order.append("collect") or [],
    )
    monkeypatch.setattr(
        review_deslop,
        "lens_model_config",
        lambda name: SimpleNamespace(
            model="gpt-5.5", reasoning_effort="medium", service_tier=None
        ),
    )

    def fake_run_codex(**kwargs):
        order.append("review")
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
    monkeypatch.setattr(
        sys, "argv", ["review_deslop.py", "--cd", str(tmp_path), "--base", "main"]
    )

    assert review_deslop.main() == 0

    assert not hasattr(review_deslop, "run_codex_review")
    assert captured["run_codex"]["review_root"] == tmp_path
    prompt = str(captured["run_codex"]["prompt"])
    assert "base branch `origin/main`" in prompt
    assert "redundant code" in prompt
    assert "=== BEGIN DIFF ===" not in prompt
    assert "diff --git" not in prompt
    assert order == ["start", "review", "collect"]


def test_main_stops_static_scan_when_review_launch_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stopped: list[object] = []
    scan = review_deslop.StaticCleanupScan(
        process=object(),
        changed_lines={},
        stdout_path=tmp_path / "stdout.txt",
        stderr_path=tmp_path / "stderr.txt",
    )

    monkeypatch.setattr(review_deslop, "resolve_repo_root", lambda cd: tmp_path)
    monkeypatch.setattr(
        review_deslop, "ensure_clean_git_worktree", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        review_deslop,
        "effective_base_ref",
        lambda review_root, base: {"base": "origin/main", "requested_base": base},
    )
    monkeypatch.setattr(
        review_deslop, "_start_static_cleanup_scan", lambda **kwargs: scan
    )
    monkeypatch.setattr(
        review_deslop,
        "_stop_static_cleanup_scan",
        lambda active_scan: stopped.append(active_scan),
    )
    monkeypatch.setattr(
        review_deslop,
        "lens_model_config",
        lambda name: SimpleNamespace(
            model="gpt-5.5", reasoning_effort="medium", service_tier=None
        ),
    )
    monkeypatch.setattr(
        review_deslop,
        "run_codex",
        lambda **kwargs: (_ for _ in ()).throw(ValueError("launch failed")),
    )
    monkeypatch.setattr(review_deslop, "emit_error", lambda *args, **kwargs: 2)
    monkeypatch.setattr(
        sys, "argv", ["review_deslop.py", "--cd", str(tmp_path), "--base", "main"]
    )

    assert review_deslop.main() == 2
    assert stopped == [scan]


def test_main_uses_generic_prompt_for_linear_commit_ranges(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(review_deslop, "resolve_repo_root", lambda cd: tmp_path)
    monkeypatch.setattr(
        review_deslop, "ensure_clean_git_worktree", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        review_deslop, "_start_static_cleanup_scan", lambda **kwargs: None
    )
    monkeypatch.setattr(review_deslop, "_collect_static_cleanup_scan", lambda scan: [])
    monkeypatch.setattr(
        review_deslop,
        "lens_model_config",
        lambda name: SimpleNamespace(
            model="gpt-5.5", reasoning_effort="medium", service_tier=None
        ),
    )

    monkeypatch.setattr(
        review_deslop,
        "validated_linear_review_range",
        lambda review_root, start, end, label: {
            "start": start,
            "end": end,
            "resolved_start": f"{start}-resolved",
            "resolved_end": f"{end}-resolved",
            "head": f"{end}-resolved",
        },
    )

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
    monkeypatch.setattr(
        sys, "argv", ["review_deslop.py", "--commit", "abc123", "def456"]
    )

    assert review_deslop.main() == 0

    prompt = str(captured["run_codex"]["prompt"])
    assert "commit range `abc123..def456`" in prompt
    assert captured["emit_result"]["result"]["final_message"] == "No findings."
    assert "=== BEGIN DIFF ===" not in prompt
    assert "diff --git" not in prompt
