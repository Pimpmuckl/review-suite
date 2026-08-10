from __future__ import annotations

import os
import sys
from pathlib import Path


DOCS_URL = "https://developers.openai.com/codex/windows#use-codex-cli-with-wsl"


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


def use_unsafe_windows_wsl_fallback(
    review_root: Path, allow_unsafe_windows_wsl_fallback: bool
) -> bool:
    return bool(allow_unsafe_windows_wsl_fallback) and is_windows_wsl_unc_path(
        review_root
    )


def windows_wsl_codex_child_env(
    review_root: Path, allow_unsafe_windows_wsl_fallback: bool
) -> dict[str, str] | None:
    if not use_unsafe_windows_wsl_fallback(
        review_root, allow_unsafe_windows_wsl_fallback
    ):
        return None
    env = os.environ.copy()
    count = int(env.get("GIT_CONFIG_COUNT", "0"))
    env[f"GIT_CONFIG_KEY_{count}"] = "safe.directory"
    env[f"GIT_CONFIG_VALUE_{count}"] = review_root.resolve().as_posix()
    env["GIT_CONFIG_COUNT"] = str(count + 1)
    return env


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
) -> None:
    if use_unsafe_windows_wsl_fallback(review_root, allow_unsafe_windows_wsl_fallback):
        return
    if is_windows_wsl_unc_path(review_root):
        raise ValueError(
            f"{tool_name} detected a WSL repo through a Windows UNC path at {review_root}. "
            f"Rerun with --wsl to authorize the Windows Codex launch for this exact repository. "
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
            f"Use a native WSL Codex executable or rerun Windows Codex against the repository UNC path with --wsl. "
            f"Docs: {DOCS_URL}"
        )
