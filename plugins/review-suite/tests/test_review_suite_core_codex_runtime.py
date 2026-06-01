import json
import sys
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from review_suite_core.codex_runtime import (
    AUTO_UNSAFE_WINDOWS_WSL_FALLBACK_ENV,
    unsafe_windows_wsl_fallback_requested,
    use_unsafe_windows_wsl_fallback,
    validate_codex_runtime,
)
from review_suite_core.lens_runtime import codex_exec_command, codex_review_command, progress_heartbeat_line, record_wrapper_session


def test_codex_exec_command_includes_service_tier_when_configured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("review_suite_core.lens_runtime.shutil.which", lambda name: "codex")
    monkeypatch.setattr("review_suite_core.lens_runtime.validate_codex_runtime", lambda **kwargs: None)

    command = codex_exec_command(
        tool_name="review-followup",
        model="gpt-5.5",
        reasoning_effort="medium",
        service_tier="fast",
        prompt="Review this.",
        output_path=tmp_path / "out.txt",
        review_root=tmp_path,
        allow_unsafe_windows_wsl_fallback=False,
    )

    assert 'service_tier="fast"' in command
    assert command.index('service_tier="fast"') < command.index("--color")


def test_codex_review_command_targets_base_with_stdin_prompt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("review_suite_core.lens_runtime.shutil.which", lambda name: "codex")
    monkeypatch.setattr("review_suite_core.lens_runtime.validate_codex_runtime", lambda **kwargs: None)

    command = codex_review_command(
        tool_name="review-deslop",
        model="gpt-5.5",
        reasoning_effort="medium",
        service_tier="fast",
        title="review-suite::deslop",
        prompt="Review for simplification.",
        review_root=tmp_path,
        base="main",
        allow_unsafe_windows_wsl_fallback=False,
    )

    assert command[:2] == ["codex", "review"]
    assert 'service_tier="fast"' in command
    assert command[-3:] == ["--base", "main", "-"]


def test_codex_review_command_uses_exec_review_for_wsl_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("review_suite_core.lens_runtime.shutil.which", lambda name: "codex")
    monkeypatch.setattr("review_suite_core.lens_runtime.validate_codex_runtime", lambda **kwargs: None)
    monkeypatch.setattr("review_suite_core.lens_runtime.use_unsafe_windows_wsl_fallback", lambda *args: True)

    command = codex_review_command(
        tool_name="review-deslop",
        model="gpt-5.5",
        reasoning_effort="medium",
        title="review-suite::deslop",
        prompt="Review for simplification.",
        review_root=tmp_path,
        commit="abc123",
        allow_unsafe_windows_wsl_fallback=True,
    )

    assert command[:5] == ["codex", "exec", "-C", str(tmp_path), "review"]
    assert "--dangerously-bypass-approvals-and-sandbox" in command
    assert command[-3:] == ["--commit", "abc123", "-"]


def test_progress_heartbeat_line_is_sparse_for_agent_output(monkeypatch: pytest.MonkeyPatch) -> None:
    class Stderr:
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr("review_suite_core.lens_runtime.sys.stderr", Stderr())

    assert progress_heartbeat_line("review-deslop", 120) == "OK 2m: deslop"


def test_progress_heartbeat_line_keeps_elapsed_for_interactive_output(monkeypatch: pytest.MonkeyPatch) -> None:
    class Stderr:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("review_suite_core.lens_runtime.sys.stderr", Stderr())

    assert progress_heartbeat_line("review-deslop", 120) == "deslop running (120s)"


def test_record_wrapper_session_writes_timestamped_entry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setattr("review_suite_core.lens_runtime.default_review_suite_state_dir", lambda: state_dir)
    monkeypatch.setattr("review_suite_core.lens_runtime._caller_thread_id", lambda: "thread-123")
    monkeypatch.setattr("review_suite_core.lens_runtime._review_branch", lambda review_root: "feature/test")

    record_wrapper_session(
        session_id="sess-123",
        tool_name="review-deslop",
        review_root=tmp_path / "repo",
        elapsed_seconds=12.3456,
    )

    payload = json.loads((state_dir / "wrapper_sessions.jsonl").read_text(encoding="utf-8"))
    assert payload["session_id"] == "sess-123"
    assert payload["tool_name"] == "review-deslop"
    assert payload["caller_thread_id"] == "thread-123"
    assert payload["branch"] == "feature/test"
    assert payload["elapsed_seconds"] == 12.346
    assert payload["recorded_at"].endswith("Z")


def test_unsafe_windows_wsl_fallback_requested_honors_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("review_suite_core.codex_runtime._env_flag_value", lambda name: "")
    assert not unsafe_windows_wsl_fallback_requested(False)

    monkeypatch.setattr("review_suite_core.codex_runtime._env_flag_value", lambda name: "1")
    assert unsafe_windows_wsl_fallback_requested(False)


def test_unsafe_windows_wsl_fallback_requested_honors_windows_user_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(AUTO_UNSAFE_WINDOWS_WSL_FALLBACK_ENV, raising=False)
    monkeypatch.setattr("review_suite_core.codex_runtime._env_flag_value", lambda name: "on")

    assert unsafe_windows_wsl_fallback_requested(False)


def test_use_unsafe_windows_wsl_fallback_honors_env_only_for_unc_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv(AUTO_UNSAFE_WINDOWS_WSL_FALLBACK_ENV, "true")

    assert use_unsafe_windows_wsl_fallback(Path("//wsl.localhost/Ubuntu/home/alice/code/repo"), False)
    assert not use_unsafe_windows_wsl_fallback(Path("C:/Code/repo"), False)


def test_validate_codex_runtime_mentions_env_opt_in_for_unc_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("review_suite_core.codex_runtime._env_flag_value", lambda name: "")

    with pytest.raises(ValueError) as excinfo:
        validate_codex_runtime(
            tool_name="review-suite",
            codex_executable="codex",
            review_root=Path("//wsl.localhost/Ubuntu/home/alice/code/repo"),
            allow_unsafe_windows_wsl_fallback=False,
            unsafe_command_hint="codex exec --dangerously-bypass-approvals-and-sandbox",
        )

    message = str(excinfo.value)
    assert "--wsl" in message
    assert f"{AUTO_UNSAFE_WINDOWS_WSL_FALLBACK_ENV}=1" in message


def test_validate_codex_runtime_mentions_windows_unc_workaround_for_wsl_windows_shim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("review_suite_core.codex_runtime.running_in_wsl", lambda: True)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")

    with pytest.raises(ValueError) as excinfo:
        validate_codex_runtime(
            tool_name="review-suite",
            codex_executable="/mnt/c/Users/alice/AppData/Roaming/npm/codex",
            review_root=Path("/home/alice/code/repo"),
            allow_unsafe_windows_wsl_fallback=False,
            unsafe_command_hint="codex exec --dangerously-bypass-approvals-and-sandbox",
        )

    message = str(excinfo.value)
    assert "//wsl.localhost/Ubuntu/home/alice/code/repo" in message
    assert "--wsl" in message
