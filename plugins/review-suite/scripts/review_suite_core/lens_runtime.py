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


DEFAULT_MODEL = "gpt-5.5"
DEFAULT_REASONING_EFFORT = "medium"
DEFAULT_PROGRESS_INTERVAL_SECONDS = 30
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
) -> dict[str, str | int | None]:
    with tempfile.NamedTemporaryFile(prefix=f"{tool_name}-message-", suffix=".txt", delete=False) as handle:
        output_path = Path(handle.name)
    with tempfile.NamedTemporaryFile(prefix=f"{tool_name}-stdout-", suffix=".txt", delete=False) as handle:
        stdout_path = Path(handle.name)
    with tempfile.NamedTemporaryFile(prefix=f"{tool_name}-stderr-", suffix=".txt", delete=False) as handle:
        stderr_path = Path(handle.name)
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
        start = time.monotonic()
        last_progress = start
        timed_out = False
        with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open("w", encoding="utf-8") as stderr_handle:
            proc = subprocess.Popen(
                command,
                cwd=str(wrapper_launch_cwd()),
                stdin=subprocess.PIPE if prompt.strip() else None,
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            print(
                f"{compact_tool_label(tool_name)} started; waiting for review result.",
                file=sys.stderr,
                flush=True,
            )
            if prompt.strip() and proc.stdin is not None:
                proc.stdin.write(prompt)
                proc.stdin.close()
            while proc.poll() is None:
                now = time.monotonic()
                elapsed = int(now - start)
                if timeout_seconds > 0 and elapsed >= timeout_seconds:
                    proc.kill()
                    timed_out = True
                    print(f"[{tool_name}] timed out after {elapsed}s", file=sys.stderr, flush=True)
                    break
                if now - last_progress >= progress_interval_seconds:
                    print(f"{compact_tool_label(tool_name)} running ({elapsed}s)", file=sys.stderr, flush=True)
                    last_progress = now
                time.sleep(1.0)
            returncode = proc.wait()
        stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else ""
        elapsed_seconds = round(time.monotonic() - start, 3)
        session_id = extract_session_id(stderr_text)
        record_wrapper_session(
            session_id=session_id,
            tool_name=tool_name,
            review_root=review_root,
            elapsed_seconds=elapsed_seconds,
        )
        return {
            "returncode": returncode,
            "stdout": stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else "",
            "stderr": stderr_text,
            "final_message": output_path.read_text(encoding="utf-8").strip() if output_path.exists() else "",
            "session_id": session_id,
            "elapsed_seconds": elapsed_seconds,
            "timed_out": timed_out,
        }
    finally:
        for path in (output_path, stdout_path, stderr_path):
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
