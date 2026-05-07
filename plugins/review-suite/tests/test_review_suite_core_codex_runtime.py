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
from review_suite_core.lens_runtime import codex_exec_command


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
