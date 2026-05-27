from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .axi_output import emit_toon
from .codex_runtime import use_unsafe_windows_wsl_fallback, validate_codex_runtime, wrapper_launch_cwd
from .process_runtime import CapturedChildProcess, launch_captured_child_process, wait_for_captured_child_process


DEFAULT_MODEL = "gpt-5.5"
DEFAULT_REASONING_EFFORT = "medium"
DEFAULT_PROGRESS_INTERVAL_SECONDS = 60
DEFAULT_TIMEOUT_SECONDS = 0
WRAPPER_SESSION_LOG_FILENAME = "wrapper_sessions.jsonl"
SUPPORTED_CODEX_SERVICE_TIERS = {"fast", "flex"}


def normalize_service_tier(value: str | None) -> str | None:
    service_tier = str(value or "").strip().lower()
    if not service_tier:
        return None
    if service_tier not in SUPPORTED_CODEX_SERVICE_TIERS:
        raise ValueError(f"service_tier must be one of: {', '.join(sorted(SUPPORTED_CODEX_SERVICE_TIERS))}")
    return service_tier


def compact_tool_label(tool_name: str) -> str:
    if tool_name.startswith("review-"):
        return tool_name[len("review-") :]
    return tool_name


def progress_heartbeat_line(tool_name: str, elapsed_seconds: int) -> str:
    label = compact_tool_label(tool_name)
    if bool(getattr(sys.stderr, "isatty", lambda: False)()):
        return f"{label} running ({elapsed_seconds}s)"
    minutes = max(1, int(elapsed_seconds) // 60)
    return f"OK {minutes}m: {label}"


def codex_exec_command(
    *,
    tool_name: str,
    model: str,
    reasoning_effort: str,
    service_tier: str | None = None,
    prompt: str,
    output_path: Path,
    review_root: Path,
    allow_unsafe_windows_wsl_fallback: bool,
) -> list[str]:
    codex_executable = shutil.which("codex") or shutil.which("codex.cmd") or "codex"
    validate_codex_runtime(
        tool_name=tool_name,
        codex_executable=codex_executable,
        review_root=review_root,
        allow_unsafe_windows_wsl_fallback=allow_unsafe_windows_wsl_fallback,
        unsafe_command_hint="codex exec --dangerously-bypass-approvals-and-sandbox",
    )
    command = [
        codex_executable,
        "exec",
        "-C",
        str(review_root),
        "-c",
        f'model="{model}"',
        "-c",
        f'model_reasoning_effort="{reasoning_effort}"',
        "--color",
        "never",
        "-o",
        str(output_path),
    ]
    normalized_service_tier = normalize_service_tier(service_tier)
    if normalized_service_tier:
        insert_at = command.index("--color")
        command[insert_at:insert_at] = ["-c", f'service_tier="{normalized_service_tier}"']
    if prompt.strip():
        command.append("-")
    if use_unsafe_windows_wsl_fallback(review_root, allow_unsafe_windows_wsl_fallback):
        command.insert(2, "--dangerously-bypass-approvals-and-sandbox")
    else:
        command[4:4] = ["-s", "read-only"]
    return command


def extract_session_id(stderr_text: str) -> str | None:
    marker = "session id:"
    for line in stderr_text.splitlines():
        if marker not in line.lower():
            continue
        _, _, tail = line.partition(":")
        value = tail.strip()
        if value:
            return value
    return None


def default_review_suite_state_dir() -> Path:
    return Path.home() / ".codex" / "state" / "review-suite"


def _caller_thread_id() -> str:
    return (os.environ.get("CODEX_THREAD_ID") or "").strip()


def _review_branch(review_root: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(review_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def record_wrapper_session(*, session_id: str | None, tool_name: str, review_root: Path, elapsed_seconds: float) -> None:
    if not session_id:
        return
    state_dir = default_review_suite_state_dir()
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "session_id": session_id,
            "tool_name": tool_name,
            "caller_thread_id": _caller_thread_id(),
            "branch": _review_branch(review_root),
            "review_cwd": str(review_root),
            "elapsed_seconds": round(float(elapsed_seconds), 3),
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        with (state_dir / WRAPPER_SESSION_LOG_FILENAME).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
    except OSError:
        return


def truncate(text: str | None, *, limit: int = 1200) -> str:
    if not text:
        return ""
    value = text.strip()
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "\n...[truncated]..."


def run_codex(
    *,
    tool_name: str,
    prompt: str,
    model: str,
    reasoning_effort: str,
    service_tier: str | None = None,
    review_root: Path,
    progress_interval_seconds: int,
    timeout_seconds: int,
    allow_unsafe_windows_wsl_fallback: bool,
) -> dict[str, object]:
    with tempfile.NamedTemporaryFile(prefix=f"{tool_name}-message-", suffix=".txt", delete=False) as handle:
        output_path = Path(handle.name)
    child: CapturedChildProcess | None = None
    try:
        command = codex_exec_command(
            tool_name=tool_name,
            model=model,
            reasoning_effort=reasoning_effort,
            service_tier=service_tier,
            prompt=prompt,
            output_path=output_path,
            review_root=review_root,
            allow_unsafe_windows_wsl_fallback=allow_unsafe_windows_wsl_fallback,
        )
        child = launch_captured_child_process(
            command=command,
            cwd=wrapper_launch_cwd(),
            stdin_text=prompt if prompt.strip() else None,
            stdout_prefix=f"{tool_name}-stdout-",
            stderr_prefix=f"{tool_name}-stderr-",
            stdout_suffix=".txt",
            stderr_suffix=".txt",
        )
        wait_result = wait_for_captured_child_process(
            process=child.process,
            started_monotonic=child.started_monotonic,
            start_line=f"{compact_tool_label(tool_name)} started; waiting for review result.",
            heartbeat_line=lambda elapsed: progress_heartbeat_line(tool_name, elapsed),
            timeout_line=lambda elapsed: f"[{tool_name}] timed out after {elapsed}s",
            progress_interval_seconds=progress_interval_seconds,
            timeout_seconds=timeout_seconds,
        )
        stderr_text = child.stderr_path.read_text(encoding="utf-8", errors="replace") if child.stderr_path.exists() else ""
        session_id = extract_session_id(stderr_text)
        record_wrapper_session(
            session_id=session_id,
            tool_name=tool_name,
            review_root=review_root,
            elapsed_seconds=wait_result.elapsed_seconds,
        )
        return {
            "returncode": wait_result.returncode,
            "stdout": child.stdout_path.read_text(encoding="utf-8", errors="replace") if child.stdout_path.exists() else "",
            "stderr": stderr_text,
            "final_message": output_path.read_text(encoding="utf-8").strip() if output_path.exists() else "",
            "session_id": session_id,
            "elapsed_seconds": wait_result.elapsed_seconds,
            "timed_out": wait_result.timed_out,
        }
    finally:
        child_paths = (child.stdout_path, child.stderr_path) if child is not None else ()
        for path in (output_path, *child_paths):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def emit_result(
    *,
    tool_name: str,
    result: dict[str, str | int | None],
) -> int:
    if int(result["returncode"]) != 0:
        payload = {
            "status": "timeout" if result.get("timed_out") else "error",
            "error": f"{tool_name} run timed out" if result.get("timed_out") else f"{tool_name} run failed",
            "session": result["session_id"],
            "elapsed_s": result.get("elapsed_seconds"),
            "stderr": truncate(str(result["stderr"])),
            "stdout": truncate(str(result["stdout"])),
        }
        emit_toon({key: value for key, value in payload.items() if value not in (None, "")})
        return int(result["returncode"])
    emit_toon(
        {
            "status": "ok",
            "session": result["session_id"],
            "elapsed_s": result.get("elapsed_seconds"),
            "review": str(result["final_message"]).strip(),
        }
    )
    return 0
