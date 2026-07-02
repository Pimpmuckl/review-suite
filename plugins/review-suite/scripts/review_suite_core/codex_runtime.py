from __future__ import annotations

import os
import sys
from pathlib import Path


DOCS_URL = "https://developers.openai.com/codex/windows#use-codex-cli-with-wsl"
AUTO_UNSAFE_WINDOWS_WSL_FALLBACK_ENV = "REVIEW_SUITE_AUTO_WSL_FALLBACK"


def wrapper_launch_cwd() -> Path:
    codex_home = Path.home() / ".codex"
    if codex_home.exists():
        return codex_home.resolve()
    return Path.home().resolve()


def effective_execution_cwd(
    review_root: Path, allow_unsafe_windows_wsl_fallback: bool
) -> Path:
    if use_unsafe_windows_wsl_fallback(review_root, allow_unsafe_windows_wsl_fallback):
        return wrapper_launch_cwd()
    return review_root.resolve()


def is_windows_wsl_unc_path(path: Path) -> bool:
    if sys.platform != "win32":
        return False
    prefixes = ("\\\\wsl.localhost\\", "\\\\wsl$\\")
    normalized = str(path).replace("/", "\\").lower()
    if normalized.startswith(prefixes):
        return True
    try:
        resolved = str(path.resolve()).replace("/", "\\").lower()
    except OSError:
        return False
    return resolved.startswith(prefixes)


def running_in_wsl() -> bool:
    if sys.platform == "win32":
        return False
    if os.environ.get("WSL_INTEROP") or os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in os.uname().release.lower()
    except AttributeError:
        return False


def _env_flag_value(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if value:
        return value
    if sys.platform != "win32":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            registry_value, _ = winreg.QueryValueEx(key, name)
        return str(registry_value or "").strip()
    except Exception:
        return ""


def _env_flag_enabled(name: str) -> bool:
    value = _env_flag_value(name).lower()
    return value in {"1", "true", "yes", "on"}


def unsafe_windows_wsl_fallback_requested(explicit_flag: bool) -> bool:
    return bool(explicit_flag) or _env_flag_enabled(
        AUTO_UNSAFE_WINDOWS_WSL_FALLBACK_ENV
    )


def use_unsafe_windows_wsl_fallback(
    review_root: Path, allow_unsafe_windows_wsl_fallback: bool
) -> bool:
    return unsafe_windows_wsl_fallback_requested(
        allow_unsafe_windows_wsl_fallback
    ) and is_windows_wsl_unc_path(review_root)


def _windows_unc_fallback_hint(review_root: Path) -> str | None:
    distro = str(os.environ.get("WSL_DISTRO_NAME") or "").strip()
    normalized = str(review_root).replace("\\", "/").strip()
    if not distro or not normalized.startswith("/"):
        return None
    return f"//wsl.localhost/{distro}{normalized}"


def validate_codex_runtime(
    *,
    tool_name: str,
    codex_executable: str,
    review_root: Path,
    allow_unsafe_windows_wsl_fallback: bool,
    unsafe_command_hint: str,
) -> None:
    if use_unsafe_windows_wsl_fallback(review_root, allow_unsafe_windows_wsl_fallback):
        return
    if is_windows_wsl_unc_path(review_root):
        raise ValueError(
            f"{tool_name} detected a WSL repo through a Windows UNC path at {review_root}. "
            f"The normal Windows Codex sandbox fails in this lane. Preferred path: use a native WSL Codex install. "
            f"If you truly need the mixed Windows lane, rerun with --wsl or set "
            f"{AUTO_UNSAFE_WINDOWS_WSL_FALLBACK_ENV}=1 to use "
            f"`{unsafe_command_hint}` from a safe Windows launch cwd. This bypasses the Codex sandbox and is not the happy path. "
            f"Docs: {DOCS_URL}"
        )
    if not running_in_wsl():
        return
    codex_text = codex_executable.replace("\\", "/").lower()
    if codex_text.startswith("/mnt/"):
        fallback_hint = _windows_unc_fallback_hint(review_root)
        fallback_text = ""
        if fallback_hint:
            fallback_text = (
                f" Current workaround on this machine: exit WSL and rerun from Windows with "
                f"`--cd {fallback_hint} --wsl`."
            )
        raise ValueError(
            f"{tool_name} detected a native WSL run, but `codex` resolves to the Windows shim at {codex_executable}. "
            f"Native WSL review is unsupported in this configuration.{fallback_text} "
            f"Durable fix: install and authenticate Codex inside WSL, then rerun from your WSL repo under /home/.... "
            f"Suggested commands: `npm i -g @openai/codex` then `codex`. Docs: {DOCS_URL}"
        )
