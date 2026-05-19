from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .axi_output import format_command
from .orchestrator_state import deslop_should_run, mark_deslop_done, mark_deslop_failed
from .paths import cwd_path_from_normalized


@dataclass(frozen=True)
class OrchestratorRunnerResult:
    state: dict[str, Any]
    ran_step: bool
    step: str | None = None


def _script_path(name: str) -> Path:
    return Path(__file__).resolve().parents[1] / name


def _identity_text(state: dict[str, Any], key: str) -> str:
    value = str(dict(state.get("identity") or {}).get(key) or "").strip()
    if not value:
        raise ValueError(f"state.identity.{key} is required")
    return value


def deslop_command(state: dict[str, Any]) -> list[str]:
    cwd = cwd_path_from_normalized(_identity_text(state, "cwd"))
    return [
        sys.executable,
        str(_script_path("review_deslop.py")),
        "--cd",
        str(cwd),
        "--base",
        _identity_text(state, "base"),
    ]


def run_deslop_subprocess(*, command: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _run_deslop_once(state: dict[str, Any]) -> OrchestratorRunnerResult:
    command = deslop_command(state)
    command_text = format_command(command)
    cwd = cwd_path_from_normalized(_identity_text(state, "cwd"))
    try:
        proc = run_deslop_subprocess(command=command, cwd=cwd)
    except OSError as exc:
        return OrchestratorRunnerResult(
            mark_deslop_failed(
                state,
                command=command_text,
                returncode=None,
                reason=f"deslop failed: {exc}",
            ),
            ran_step=True,
            step="deslop",
        )
    if int(proc.returncode) == 0:
        return OrchestratorRunnerResult(mark_deslop_done(state, command=command_text), ran_step=True, step="deslop")
    return OrchestratorRunnerResult(
        mark_deslop_failed(
            state,
            command=command_text,
            returncode=int(proc.returncode),
            reason=f"deslop failed with exit {int(proc.returncode)}",
        ),
        ran_step=True,
        step="deslop",
    )


def run_one_expensive_step(state: dict[str, Any]) -> OrchestratorRunnerResult:
    if deslop_should_run(state):
        return _run_deslop_once(state)
    return OrchestratorRunnerResult(state, ran_step=False)
