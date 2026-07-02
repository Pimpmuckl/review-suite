from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class CapturedChildProcess:
    process: subprocess.Popen[str]
    stdout_path: Path
    stderr_path: Path
    started_monotonic: float


@dataclass(frozen=True)
class CapturedChildWaitResult:
    returncode: int
    timed_out: bool
    elapsed_seconds: float


def launch_captured_child_process(
    *,
    command: list[str],
    cwd: Path,
    stdin_text: str | None = None,
    stdout_prefix: str,
    stderr_prefix: str | None = None,
    stdout_suffix: str = ".stdout.txt",
    stderr_suffix: str = ".stderr.txt",
) -> CapturedChildProcess:
    stdout_tmp = tempfile.NamedTemporaryFile(
        prefix=stdout_prefix, suffix=stdout_suffix, delete=False
    )
    stderr_tmp = tempfile.NamedTemporaryFile(
        prefix=stderr_prefix or stdout_prefix, suffix=stderr_suffix, delete=False
    )
    stdout_path = Path(stdout_tmp.name)
    stderr_path = Path(stderr_tmp.name)
    started_monotonic = time.monotonic()
    try:
        proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdin=subprocess.PIPE if stdin_text else None,
            stdout=stdout_tmp,
            stderr=stderr_tmp,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if stdin_text and proc.stdin is not None:
            proc.stdin.write(stdin_text)
            proc.stdin.close()
        return CapturedChildProcess(
            process=proc,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            started_monotonic=started_monotonic,
        )
    except Exception:
        stdout_tmp.close()
        stderr_tmp.close()
        for path in (stdout_path, stderr_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    finally:
        stdout_tmp.close()
        stderr_tmp.close()


def wait_for_captured_child_process(
    *,
    process: subprocess.Popen[str],
    started_monotonic: float,
    start_line: str | None,
    heartbeat_line: Callable[[int], str],
    timeout_line: Callable[[int], str],
    progress_interval_seconds: int,
    timeout_seconds: int,
    poll_interval_seconds: float = 1.0,
) -> CapturedChildWaitResult:
    last_progress = started_monotonic
    timed_out = False
    if start_line:
        print(start_line, file=sys.stderr, flush=True)
    while process.poll() is None:
        now = time.monotonic()
        elapsed = int(now - started_monotonic)
        if timeout_seconds > 0 and elapsed >= timeout_seconds:
            process.kill()
            timed_out = True
            print(timeout_line(elapsed), file=sys.stderr, flush=True)
            break
        if (
            progress_interval_seconds >= 0
            and now - last_progress >= progress_interval_seconds
        ):
            print(heartbeat_line(elapsed), file=sys.stderr, flush=True)
            last_progress = now
        time.sleep(max(0.0, poll_interval_seconds))
    returncode = process.wait()
    return CapturedChildWaitResult(
        returncode=returncode,
        timed_out=timed_out,
        elapsed_seconds=round(time.monotonic() - started_monotonic, 3),
    )
