from __future__ import annotations

import sys
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from review_suite_core.process_runtime import (
    launch_captured_child_process,
    wait_for_captured_child_process,
)


def test_launch_captured_child_process_starts_clock_before_stdin_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class FakeStdin:
        def write(self, value: str) -> None:
            events.append(f"write:{value}")

        def close(self) -> None:
            events.append("close")

    class FakeProcess:
        stdin = FakeStdin()

    def fake_monotonic() -> float:
        events.append("clock")
        return 123.0

    def fake_popen(*args, **kwargs) -> FakeProcess:
        events.append("popen")
        return FakeProcess()

    monkeypatch.setattr(
        "review_suite_core.process_runtime.time.monotonic", fake_monotonic
    )
    monkeypatch.setattr(
        "review_suite_core.process_runtime.subprocess.Popen", fake_popen
    )

    child = launch_captured_child_process(
        command=[sys.executable, "-c", "pass"],
        cwd=tmp_path,
        stdin_text="prompt",
        stdout_prefix="child-stdout-",
        stderr_prefix="child-stderr-",
    )
    try:
        assert child.started_monotonic == 123.0
        assert events[:4] == ["clock", "popen", "write:prompt", "close"]
    finally:
        child.stdout_path.unlink(missing_ok=True)
        child.stderr_path.unlink(missing_ok=True)


def test_wait_for_captured_child_process_keeps_child_stderr_captured(
    capsys,
    tmp_path: Path,
) -> None:
    child = launch_captured_child_process(
        command=[
            sys.executable,
            "-c",
            "import sys, time; print('child stdout'); print('child stderr', file=sys.stderr); time.sleep(0.05)",
        ],
        cwd=tmp_path,
        stdout_prefix="child-stdout-",
        stderr_prefix="child-stderr-",
        stdout_suffix=".txt",
        stderr_suffix=".txt",
    )
    try:
        result = wait_for_captured_child_process(
            process=child.process,
            started_monotonic=child.started_monotonic,
            start_line="[parent] waiting for child",
            heartbeat_line=lambda elapsed: f"OK {max(1, elapsed // 60)}m: child",
            timeout_line=lambda elapsed: f"child timed out after {elapsed}s",
            progress_interval_seconds=0,
            timeout_seconds=0,
            poll_interval_seconds=0.01,
        )
        captured = capsys.readouterr()

        assert result.returncode == 0
        assert "child stdout" in child.stdout_path.read_text(encoding="utf-8")
        assert "child stderr" in child.stderr_path.read_text(encoding="utf-8")
        assert "[parent] waiting for child" in captured.err
        assert "OK 1m: child" in captured.err
        assert "child stderr" not in captured.err
    finally:
        child.stdout_path.unlink(missing_ok=True)
        child.stderr_path.unlink(missing_ok=True)
