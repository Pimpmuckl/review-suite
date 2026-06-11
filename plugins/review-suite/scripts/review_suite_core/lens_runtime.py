from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .axi_output import emit_toon
from .codex_runtime import use_unsafe_windows_wsl_fallback, validate_codex_runtime, wrapper_launch_cwd
from .process_runtime import CapturedChildProcess, launch_captured_child_process, wait_for_captured_child_process
from .workflow_state import validated_linear_review_range


DEFAULT_MODEL = "gpt-5.5"
DEFAULT_REASONING_EFFORT = "medium"
DEFAULT_PROGRESS_INTERVAL_SECONDS = 60
DEFAULT_TIMEOUT_SECONDS = 0
WRAPPER_SESSION_LOG_FILENAME = "wrapper_sessions.jsonl"
SUPPORTED_CODEX_SERVICE_TIERS = {"fast", "flex"}


@dataclass(frozen=True)
class CodexReviewLaunch:
    command: list[str]
    stdin_text: str | None
    final_message_path: Path | None
    cwd: Path


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


def _codex_command_prefix(
    *,
    tool_name: str,
    review_root: Path,
    allow_unsafe_windows_wsl_fallback: bool,
    unsafe_command_hint: str,
    subcommand: str,
    model: str,
    reasoning_effort: str,
    service_tier: str | None,
    title: str | None = None,
) -> list[str]:
    codex_executable = shutil.which("codex") or shutil.which("codex.cmd") or "codex"
    validate_codex_runtime(
        tool_name=tool_name,
        codex_executable=codex_executable,
        review_root=review_root,
        allow_unsafe_windows_wsl_fallback=allow_unsafe_windows_wsl_fallback,
        unsafe_command_hint=unsafe_command_hint,
    )
    unsafe_fallback = use_unsafe_windows_wsl_fallback(review_root, allow_unsafe_windows_wsl_fallback)
    command = [codex_executable]
    if subcommand == "exec":
        command.append("exec")
        if unsafe_fallback:
            command.append("--dangerously-bypass-approvals-and-sandbox")
        command.extend(["-C", str(review_root)])
        if not unsafe_fallback:
            command.extend(["-s", "read-only"])
    elif subcommand == "exec-review":
        command.append("exec")
        if unsafe_fallback:
            command.append("--dangerously-bypass-approvals-and-sandbox")
        command.extend(["-C", str(review_root)])
        if not unsafe_fallback:
            command.extend(["-s", "read-only"])
        command.append("review")
    else:
        raise ValueError(f"unsupported Codex subcommand: {subcommand}")
    command.extend(
        [
            "-c",
            f'model="{model}"',
            "-c",
            f'model_reasoning_effort="{reasoning_effort}"',
        ]
    )
    normalized_service_tier = normalize_service_tier(service_tier)
    if normalized_service_tier:
        command.extend(["-c", f'service_tier="{normalized_service_tier}"'])
    if title is not None:
        command.extend(["--title", title])
    return command


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
    output_path: Path | None = None,
    review_root: Path,
    allow_unsafe_windows_wsl_fallback: bool,
) -> list[str]:
    command = _codex_command_prefix(
        tool_name=tool_name,
        review_root=review_root,
        allow_unsafe_windows_wsl_fallback=allow_unsafe_windows_wsl_fallback,
        unsafe_command_hint="codex exec --dangerously-bypass-approvals-and-sandbox",
        subcommand="exec",
        model=model,
        reasoning_effort=reasoning_effort,
        service_tier=service_tier,
    )
    command.extend(["--color", "never"])
    if output_path is not None:
        command.extend(["-o", str(output_path)])
    if prompt.strip():
        command.append("-")
    return command


def codex_review_stdin_text(
    *,
    prompt: str,
    base: str | None = None,
    commit: str | None = None,
    commit_end: str | None = None,
) -> str | None:
    review_prompt = prompt.strip()
    base_ref = str(base or "").strip()
    commit_ref = str(commit or "").strip()
    commit_end_ref = str(commit_end or "").strip()
    if not review_prompt and not commit_end_ref:
        return None
    if commit_end_ref:
        if not base_ref or commit_ref:
            raise ValueError("commit-range review prompt requires base and commit_end")
        target = (
            f"Review target: review commit range `{base_ref}..{commit_end_ref}`. "
            "Use local git commands to inspect that bounded range; no inline diff is provided."
        )
    elif bool(base_ref) == bool(commit_ref):
        raise ValueError("targeted review prompt requires exactly one of base or commit")
    elif base_ref:
        target = (
            f"Review target: compare the current checkout against base ref `{base_ref}`. "
            "Use local git commands to inspect the diff; no inline diff is provided."
        )
    else:
        target = (
            f"Review target: review the changes introduced by commit `{commit_ref}`. "
            "Use local git commands to inspect the commit; no inline diff is provided."
        )
    message = f"You are running a focused code review. Do not modify files.\n{target}\n"
    if review_prompt:
        message += f"\nReview instructions:\n{review_prompt}\n"
    return message


def codex_exec_review_command(
    *,
    tool_name: str,
    model: str,
    reasoning_effort: str,
    service_tier: str | None = None,
    title: str,
    review_root: Path,
    base: str | None = None,
    commit: str | None = None,
    prompt: str = "",
    output_path: Path | None = None,
    allow_unsafe_windows_wsl_fallback: bool,
) -> list[str]:
    base_ref = str(base or "").strip()
    commit_ref = str(commit or "").strip()
    prompt_text = prompt.strip()
    if base_ref and commit_ref:
        raise ValueError("native exec review requires at most one of base or commit")
    if (base_ref or commit_ref) and prompt_text:
        raise ValueError("native exec review cannot combine --base/--commit with a custom prompt")
    if not (base_ref or commit_ref or prompt_text):
        raise ValueError("exec review requires --base, --commit, or a custom prompt")
    command = _codex_command_prefix(
        tool_name=tool_name,
        review_root=review_root,
        allow_unsafe_windows_wsl_fallback=allow_unsafe_windows_wsl_fallback,
        unsafe_command_hint="codex exec --dangerously-bypass-approvals-and-sandbox review",
        subcommand="exec-review",
        model=model,
        reasoning_effort=reasoning_effort,
        service_tier=service_tier,
        title=title,
    )
    if output_path is not None:
        command.extend(["-o", str(output_path)])
    if base_ref:
        command.extend(["--base", base_ref])
    elif commit_ref:
        command.extend(["--commit", commit_ref])
    else:
        command.append("-")
    return command


def _review_output_path(prefix: str) -> Path:
    output_dir = default_review_suite_state_dir() / "tmp"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir / f"{prefix}{uuid.uuid4().hex}.txt"


def prepare_codex_review_launch(
    *,
    tool_name: str,
    model: str,
    reasoning_effort: str,
    service_tier: str | None = None,
    title: str,
    review_root: Path,
    base: str | None = None,
    commit: str | None = None,
    commit_end: str | None = None,
    prompt: str = "",
    output_prefix: str | None = None,
    allow_unsafe_windows_wsl_fallback: bool,
) -> CodexReviewLaunch:
    base_ref = str(base or "").strip()
    commit_end_ref = str(commit_end or "").strip()
    if base_ref and commit_end_ref:
        validated_linear_review_range(
            review_root,
            base_ref,
            commit_end_ref,
            label="native commit-range review launch",
        )
    stdin_text = codex_review_stdin_text(prompt=prompt, base=base, commit=commit, commit_end=commit_end)
    final_message_path: Path | None = None
    if stdin_text is not None:
        prefix = output_prefix or f"{tool_name}-message-"
        final_message_path = _review_output_path(prefix)
    command_kwargs = {
        "tool_name": tool_name,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "service_tier": service_tier,
        "title": title,
        "output_path": final_message_path,
        "review_root": review_root,
        "allow_unsafe_windows_wsl_fallback": allow_unsafe_windows_wsl_fallback,
    }
    if stdin_text is not None:
        # Codex rejects native --base/--commit targets when custom review instructions are supplied.
        # Keep the review subcommand, but put the git target in stdin with the lens instructions.
        command_kwargs["prompt"] = stdin_text
    else:
        command_kwargs["base"] = base
        command_kwargs["commit"] = commit
    command = codex_exec_review_command(**command_kwargs)
    cwd = (
        wrapper_launch_cwd()
        if use_unsafe_windows_wsl_fallback(review_root, allow_unsafe_windows_wsl_fallback)
        else review_root
    )
    return CodexReviewLaunch(
        command=command,
        stdin_text=stdin_text,
        final_message_path=final_message_path,
        cwd=cwd,
    )


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


def _run_captured_codex_command(
    *,
    tool_name: str,
    command: list[str],
    cwd: Path,
    stdin_text: str | None,
    review_root: Path,
    progress_interval_seconds: int,
    timeout_seconds: int,
    final_message_path: Path | None = None,
    cleanup_paths: tuple[Path, ...] = (),
) -> dict[str, object]:
    child: CapturedChildProcess | None = None
    try:
        child = launch_captured_child_process(
            command=command,
            cwd=cwd,
            stdin_text=stdin_text,
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
        stdout_text = child.stdout_path.read_text(encoding="utf-8", errors="replace") if child.stdout_path.exists() else ""
        stderr_text = child.stderr_path.read_text(encoding="utf-8", errors="replace") if child.stderr_path.exists() else ""
        session_id = extract_session_id(stderr_text)
        record_wrapper_session(
            session_id=session_id,
            tool_name=tool_name,
            review_root=review_root,
            elapsed_seconds=wait_result.elapsed_seconds,
        )
        final_message = stdout_text.strip()
        if final_message_path is not None:
            final_message = final_message_path.read_text(encoding="utf-8").strip() if final_message_path.exists() else ""
        return {
            "returncode": wait_result.returncode,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "final_message": final_message,
            "session_id": session_id,
            "elapsed_seconds": wait_result.elapsed_seconds,
            "timed_out": wait_result.timed_out,
        }
    finally:
        child_paths = (child.stdout_path, child.stderr_path) if child is not None else ()
        for path in (*cleanup_paths, *child_paths):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


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
    return _run_captured_codex_command(
        tool_name=tool_name,
        command=command,
        cwd=wrapper_launch_cwd(),
        stdin_text=prompt if prompt.strip() else None,
        review_root=review_root,
        progress_interval_seconds=progress_interval_seconds,
        timeout_seconds=timeout_seconds,
        final_message_path=output_path,
        cleanup_paths=(output_path,),
    )


def run_codex_review(
    *,
    tool_name: str,
    prompt: str,
    model: str,
    reasoning_effort: str,
    service_tier: str | None = None,
    title: str,
    review_root: Path,
    base: str | None = None,
    commit: str | None = None,
    progress_interval_seconds: int,
    timeout_seconds: int,
    allow_unsafe_windows_wsl_fallback: bool,
) -> dict[str, object]:
    launch = prepare_codex_review_launch(
        tool_name=tool_name,
        model=model,
        reasoning_effort=reasoning_effort,
        service_tier=service_tier,
        title=title,
        review_root=review_root,
        base=base,
        commit=commit,
        prompt=prompt,
        allow_unsafe_windows_wsl_fallback=allow_unsafe_windows_wsl_fallback,
    )
    return _run_captured_codex_command(
        tool_name=tool_name,
        command=launch.command,
        cwd=launch.cwd,
        stdin_text=launch.stdin_text,
        review_root=review_root,
        progress_interval_seconds=progress_interval_seconds,
        timeout_seconds=timeout_seconds,
        final_message_path=launch.final_message_path,
        cleanup_paths=(launch.final_message_path,) if launch.final_message_path is not None else (),
    )


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
