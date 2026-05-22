from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from review_gate import run_gate_round
from review_followup import build_followup_prompt
from review_suite_arena import run_orchestrator_followup_review_step, run_orchestrator_review_step
from review_suite_local import build_local_review_request, build_phase_instructions, default_roster_path

from .axi_output import format_command, write_text
from .config import lens_model_config
from .lens_runtime import DEFAULT_PROGRESS_INTERVAL_SECONDS, DEFAULT_TIMEOUT_SECONDS
from .orchestrator_state import (
    STAGE_CREATED,
    STAGE_DECISION_PENDING,
    STAGE_FOLLOWUP_PENDING,
    STAGE_GATE_RERUN_NEEDED,
    deslop_is_ready,
    deslop_should_run,
    mark_blocked,
    mark_deslop_done,
    mark_deslop_failed,
    mark_followup_review_pending,
    mark_gate_step_pending,
    mark_review_step_pending,
    next_review_profile_step,
    review_profile_has_next_step,
)
from .paths import cwd_path_from_normalized
from .workflow_state import current_head, diff_artifact, diff_paths_between, dirty_worktree_scope, merge_base


INITIAL_REVIEW_LANE = "review_t1"
FOLLOWUP_REVIEW_LANE = "review-followup"
GATE_LANES_BY_TASK_CLASS = {
    "phase_gate": "review_t2",
    "pr_gate": "review_t4",
}


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
        "--output-only",
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


def _print_step_output(*, label: str, body: str, status: str = "completed") -> bool:
    text = body.strip()
    if not text:
        return False
    heading = f"{label}:" if status == "completed" else f"{label} [{status}]:"
    write_text("Output:")
    write_text(heading)
    write_text(text)
    return True


def _process_output(proc: subprocess.CompletedProcess) -> str:
    return str(proc.stdout or "").strip() or str(proc.stderr or "").strip()


def _run_deslop_once(state: dict[str, Any]) -> OrchestratorRunnerResult:
    command = deslop_command(state)
    command_text = format_command(command)
    cwd = cwd_path_from_normalized(_identity_text(state, "cwd"))
    try:
        proc = run_deslop_subprocess(command=command, cwd=cwd)
    except OSError as exc:
        _print_step_output(label="review-deslop", status="failed", body=f"review-deslop failed: {exc}")
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
        _print_step_output(label="review-deslop", body=str(proc.stdout or ""))
        return OrchestratorRunnerResult(mark_deslop_done(state, command=command_text), ran_step=True, step="deslop")
    _print_step_output(
        label="review-deslop",
        status="failed",
        body=_process_output(proc) or f"review-deslop failed with exit {int(proc.returncode)}",
    )
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


def _review_should_run(state: dict[str, Any]) -> bool:
    if state.get("stage") != STAGE_CREATED:
        return False
    if not deslop_is_ready(state):
        return False
    pending_kind = dict(state.get("pending_action") or {}).get("kind")
    return pending_kind in (None, "resume-after-deslop", "run-review-step") and review_profile_has_next_step(state)


def _step_kind(step: dict[str, Any]) -> str:
    return str(step.get("kind") or "review").strip() or "review"


def _next_profile_step(state: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    step_index, step = next_review_profile_step(state)
    if not str(step.get("name") or "").strip():
        raise ValueError(f"state.review_plan.steps[{step_index}].name is required")
    kind = _step_kind(step)
    if kind == "gate":
        gate = str(step.get("gate") or "").strip()
        if gate not in GATE_LANES_BY_TASK_CLASS:
            raise ValueError(
                f"state.review_plan.steps[{step_index}].gate must be one of: {', '.join(GATE_LANES_BY_TASK_CLASS)}"
            )
        step["kind"] = kind
        step["gate"] = gate
        return step_index, step
    if kind != "review":
        raise ValueError(f"state.review_plan.steps[{step_index}].kind must be review or gate")
    for key in ("count", "model", "reasoning_effort"):
        if not str(step.get(key) or "").strip():
            raise ValueError(f"state.review_plan.steps[{step_index}].{key} is required")
    step["kind"] = kind
    step["count"] = int(step["count"])
    if int(step["count"]) <= 0:
        raise ValueError(f"state.review_plan.steps[{step_index}].count must be > 0")
    return step_index, step


def _identity_cwd(state: dict[str, Any]) -> Path:
    return cwd_path_from_normalized(_identity_text(state, "cwd"))


def _review_scope(state: dict[str, Any], cwd: Path) -> dict[str, object]:
    base = _identity_text(state, "base")
    try:
        reviewed_head = current_head(cwd)
    except ValueError:
        reviewed_head = _identity_text(state, "head")
    try:
        merge_base_head = merge_base(cwd, base, "HEAD")
    except ValueError:
        merge_base_head = _identity_text(state, "merge_base")
    return {
        "base": base,
        "merge_base": merge_base_head,
        "reviewed_head": reviewed_head,
        "target_label": f"base `{base}`",
    }


def _task_id(state: dict[str, Any]) -> str | None:
    branch = str(dict(state.get("identity") or {}).get("branch") or "").strip()
    return branch or None


def _review_step_position(state: dict[str, Any], step_index: int) -> tuple[int | None, int | None]:
    steps = [item for item in list(dict(state.get("review_plan") or {}).get("steps") or []) if isinstance(item, dict)]
    review_indices = [index for index, item in enumerate(steps) if _step_kind(item) == "review"]
    if step_index not in review_indices:
        return None, None
    return review_indices.index(step_index) + 1, len(review_indices)


def _attach_review_result(state: dict[str, Any], review_result: dict[str, object]) -> dict[str, Any]:
    round_id = str(review_result.get("round_id") or "").strip()
    for item in list(state.get("rounds") or []):
        if not isinstance(item, dict) or str(item.get("round_id") or "") != round_id:
            continue
        item["review_status"] = str(review_result.get("status") or "")
        item["review_blocked"] = bool(review_result.get("blocked"))
        output_refs = [str(ref) for ref in list(review_result.get("output_refs") or []) if str(ref).strip()]
        if not output_refs:
            output_refs = [
                str(run.get("ref") or "")
                for run in list(review_result.get("runs") or [])
                if isinstance(run, dict) and str(run.get("ref") or "").strip()
            ]
        if output_refs:
            item["output_refs"] = output_refs
        runs = [dict(run) for run in list(review_result.get("runs") or []) if isinstance(run, dict)]
        if runs:
            item["runs"] = runs
        round_state_dir = str(review_result.get("round_state_dir") or "").strip()
        if round_state_dir:
            item["round_state_dir"] = round_state_dir
        if bool(review_result.get("grading_required")):
            item["grading_required"] = True
        if bool(review_result.get("signoff_required")):
            item["signoff_required"] = True
        break
    return state


def run_review_step(**kwargs: Any) -> dict[str, object]:
    return run_orchestrator_review_step(**kwargs)


def run_followup_review_step(**kwargs: Any) -> dict[str, object]:
    return run_orchestrator_followup_review_step(**kwargs)


def run_gate_step(**kwargs: Any) -> tuple[dict[str, Any], int]:
    return run_gate_round(**kwargs)


def _run_profile_review_once(
    state: dict[str, Any],
    *,
    state_dir: Path,
    step_index: int | None = None,
    step: dict[str, Any] | None = None,
) -> OrchestratorRunnerResult:
    if step_index is None or step is None:
        step_index, step = _next_profile_step(state)
    if _step_kind(step) != "review":
        raise ValueError("profile step is not a review step")
    cwd = _identity_cwd(state)
    scope = _review_scope(state, cwd)
    step_position, step_total = _review_step_position(state, step_index)
    review_result = run_review_step(
        lane=INITIAL_REVIEW_LANE,
        step_name=str(step["name"]),
        step_position=step_position,
        step_total=step_total,
        reviewer_count=int(step["count"]),
        model=str(step["model"]),
        reasoning_effort=str(step["reasoning_effort"]),
        service_tier=str(step.get("service_tier") or "").strip() or None,
        review_cwd=cwd,
        state_dir=state_dir,
        sqlite_path=Path.home() / ".codex" / "state_5.sqlite",
        review_scope=scope,
        task_id=_task_id(state),
        allow_dirty=False,
        progress_interval_seconds=DEFAULT_PROGRESS_INTERVAL_SECONDS,
        allow_unsafe_windows_wsl_fallback=False,
        grading_required=bool(dict(state.get("grading") or {}).get("required")),
    )
    round_id = str(review_result.get("round_id") or "").strip()
    if not round_id:
        raise ValueError("review step did not return a round_id")
    reviewed_head = str(review_result.get("reviewed_head") or scope.get("reviewed_head") or "").strip() or None
    next_state = mark_review_step_pending(
        state,
        round_id=round_id,
        lane=INITIAL_REVIEW_LANE,
        step_index=step_index,
        step_name=str(step["name"]),
        reviewed_head=reviewed_head,
    )
    return OrchestratorRunnerResult(
        _attach_review_result(next_state, review_result),
        ran_step=True,
        step="review",
    )


def _gate_output_refs(runs: list[object]) -> list[str]:
    return [
        str(run.get("ref") or "")
        for run in runs
        if isinstance(run, dict) and str(run.get("ref") or "").strip()
    ]


def _gate_review_scope_and_prompt(*, state: dict[str, Any], cwd: Path) -> tuple[dict[str, object], str]:
    request = build_local_review_request(
        review_cwd=cwd,
        base=_identity_text(state, "base"),
        commit_values=None,
        instruction_builder=build_phase_instructions,
        custom_instructions=None,
    )
    return request.review_scope, request.prompt


def _gate_rerun_step(state: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    pending = dict(state.get("pending_action") or {})
    gate = str(pending.get("gate") or "").strip()
    if gate not in GATE_LANES_BY_TASK_CLASS:
        raise ValueError("gate rerun requires a supported gate")
    progress = dict(state.get("review_progress") or {})
    step_index = int(pending.get("step_index") if pending.get("step_index") is not None else progress.get("next_step_index", 0))
    return step_index, {
        "kind": "gate",
        "name": str(pending.get("step") or gate),
        "gate": gate,
    }


def _run_profile_gate_once(
    state: dict[str, Any],
    *,
    state_dir: Path,
    step_index: int | None = None,
    step: dict[str, Any] | None = None,
) -> OrchestratorRunnerResult:
    if step_index is None or step is None:
        step_index, step = _next_profile_step(state)
    if _step_kind(step) != "gate":
        raise ValueError("profile step is not a gate step")
    gate_task_class = str(step.get("gate") or "").strip()
    lane = GATE_LANES_BY_TASK_CLASS.get(gate_task_class)
    if lane is None:
        raise ValueError(f"unsupported gate task class: {gate_task_class}")
    cwd = _identity_cwd(state)
    review_scope, prompt = _gate_review_scope_and_prompt(state=state, cwd=cwd)
    payload, exit_code = run_gate_step(
        gate_task_class=gate_task_class,
        review_cwd=cwd,
        roster_path=default_roster_path(),
        state_dir=state_dir,
        sqlite_path=Path.home() / ".codex" / "state_5.sqlite",
        task_id=_task_id(state),
        allow_dirty=False,
        progress_interval_seconds=DEFAULT_PROGRESS_INTERVAL_SECONDS,
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        allow_unsafe_windows_wsl_fallback=False,
        review_scope=review_scope,
        prompt=prompt,
    )
    round_id = str(payload.get("round_id") or "").strip()
    if not round_id:
        raise ValueError("gate step did not return a round_id")
    reviewed_head = str(review_scope.get("reviewed_head") or "").strip() or None
    if str(payload.get("status") or "") != "signoff_pending":
        return OrchestratorRunnerResult(
            mark_blocked(
                state,
                reason=f"{lane} gate did not reach pending signoff (exit {exit_code})",
                round_id=round_id,
            ),
            ran_step=True,
            step="gate",
        )
    next_state = mark_gate_step_pending(
        state,
        round_id=round_id,
        lane=lane,
        gate=gate_task_class,
        step_index=step_index,
        step_name=str(step["name"]),
        reviewed_head=reviewed_head,
    )
    gate_result = {
        "round_id": round_id,
        "lane": lane,
        "kind": "gate",
        "status": payload.get("status"),
        "blocked": bool(payload.get("blocked")),
        "reviewed_head": reviewed_head,
        "output_refs": _gate_output_refs(list(payload.get("runs") or [])),
        "runs": list(payload.get("runs") or []),
        "signoff_required": bool(payload.get("signoff_required")),
    }
    return OrchestratorRunnerResult(
        _attach_review_result(next_state, gate_result),
        ran_step=True,
        step="gate",
    )


def _active_findings(state: dict[str, Any]) -> dict[str, Any]:
    active = state.get("active_findings")
    if not isinstance(active, dict):
        raise ValueError("follow-up review requires active findings")
    return active


def _round_by_id(state: dict[str, Any], round_id: str) -> dict[str, Any]:
    for item in list(state.get("rounds") or []):
        if isinstance(item, dict) and item.get("round_id") == round_id:
            return dict(item)
    return {}


def _followup_note(state: dict[str, Any], active: dict[str, Any], source_round_id: str) -> str:
    source_round = _round_by_id(state, source_round_id)
    refs = [str(ref) for ref in list(source_round.get("output_refs") or []) if str(ref).strip()]
    parts = [
        f"Source review round {source_round_id} was closed as findings.",
        "Review only whether the fix interdiff addresses the valid findings without introducing regressions.",
    ]
    lane = str(source_round.get("lane") or active.get("lane") or "").strip()
    if lane:
        parts.insert(1, f"Source lane: {lane}.")
    if refs:
        parts.append(f"Source reviewer output refs: {', '.join(refs)}.")
    return " ".join(parts)


def _followup_diff_artifact(*, cwd: Path, since_head: str, base: str, merge_base_head: str) -> tuple[str, bool]:
    committed_diff = diff_artifact(cwd, since_head, "HEAD")
    dirty_scope = dirty_worktree_scope(cwd, base, merge_base_ref=merge_base_head)
    dirty_paths = [
        str(path)
        for path in list(dirty_scope.get("dirty_paths") or [])
        if str(path).strip()
    ]
    related_dirty_paths = [
        str(path)
        for path in list(dirty_scope.get("related_dirty_paths") or [])
        if str(path).strip()
    ]
    if committed_diff.strip():
        interdiff_paths = diff_paths_between(cwd, since_head, "HEAD")
        related_dirty_paths = sorted(set(related_dirty_paths) | (set(dirty_paths) & interdiff_paths))
        if related_dirty_paths:
            raise ValueError(
                "follow-up review found committed interdiff changes plus uncommitted changes in reviewed paths. "
                "Commit the remaining fix changes or stash unrelated worktree changes, then rerun the emitted review.py --id command."
            )
        return committed_diff, bool(dirty_paths)
    if dirty_paths:
        raise ValueError(
            f"follow-up review found no committed interdiff after reviewed head {since_head}, "
            "but the worktree has uncommitted changes. Commit the fix changes, then rerun the emitted review.py --id command."
        )
    raise ValueError(
        f"follow-up review requires a non-empty diff after reviewed head {since_head}. "
        "Commit the fixes, then rerun the emitted review.py --id command."
    )


def _run_followup_review_once(state: dict[str, Any], *, state_dir: Path) -> OrchestratorRunnerResult:
    active = _active_findings(state)
    source_round_id = str(active.get("round_id") or "").strip()
    if not source_round_id:
        raise ValueError("active_findings.round_id is required")
    since_head = str(active.get("reviewed_head") or "").strip()
    if not since_head:
        raise ValueError("active_findings.reviewed_head is required")
    cwd = _identity_cwd(state)
    head = current_head(cwd)
    base = _identity_text(state, "base")
    try:
        merge_base_head = merge_base(cwd, base, "HEAD")
    except ValueError:
        merge_base_head = _identity_text(state, "merge_base")
    diff_text, allow_unrelated_dirty_paths = _followup_diff_artifact(cwd=cwd, since_head=since_head, base=base, merge_base_head=merge_base_head)
    review_scope = {
        "base": base,
        "commit": since_head,
        "commit_end": head,
        "reviewed_head": head,
        "merge_base": merge_base_head,
        "manual_prompt_mode": True,
        "target_label": f"interdiff `{since_head}..{head}`",
        "source_round_id": source_round_id,
    }
    if allow_unrelated_dirty_paths:
        review_scope["allow_unrelated_dirty_paths"] = True
    prompt = build_followup_prompt(
        since_head=since_head,
        head=head,
        note=_followup_note(state, active, source_round_id),
        diff_text=diff_text,
    )
    if not prompt.strip():
        raise ValueError("follow-up review prompt must not be empty")
    model_config = lens_model_config(FOLLOWUP_REVIEW_LANE, state_dir=state_dir)
    review_result = run_followup_review_step(
        model=model_config.model,
        reasoning_effort=model_config.reasoning_effort,
        service_tier=model_config.service_tier,
        review_cwd=cwd,
        state_dir=state_dir,
        sqlite_path=Path.home() / ".codex" / "state_5.sqlite",
        review_scope=review_scope,
        prompt=prompt,
        task_id=_task_id(state),
        progress_interval_seconds=DEFAULT_PROGRESS_INTERVAL_SECONDS,
        allow_unsafe_windows_wsl_fallback=False,
    )
    round_id = str(review_result.get("round_id") or "").strip()
    if not round_id:
        raise ValueError("follow-up review did not return a round_id")
    reviewed_head = str(review_result.get("reviewed_head") or head).strip() or head
    next_state = mark_followup_review_pending(
        state,
        round_id=round_id,
        reviewed_head=reviewed_head,
        source_round_id=source_round_id,
    )
    return OrchestratorRunnerResult(
        _attach_review_result(next_state, review_result),
        ran_step=True,
        step=FOLLOWUP_REVIEW_LANE,
    )


def run_one_expensive_step(state: dict[str, Any], *, state_dir: Path | None = None) -> OrchestratorRunnerResult:
    resolved_state_dir = state_dir or Path.home() / ".codex" / "state" / "review-suite"
    if deslop_should_run(state):
        return _run_deslop_once(state)
    if _review_should_run(state):
        step_index, step = _next_profile_step(state)
        if _step_kind(step) == "gate":
            return _run_profile_gate_once(state, state_dir=resolved_state_dir, step_index=step_index, step=step)
        return _run_profile_review_once(state, state_dir=resolved_state_dir, step_index=step_index, step=step)
    if state.get("stage") == STAGE_FOLLOWUP_PENDING:
        return _run_followup_review_once(state, state_dir=resolved_state_dir)
    if state.get("stage") == STAGE_GATE_RERUN_NEEDED:
        step_index, step = _gate_rerun_step(state)
        return _run_profile_gate_once(state, state_dir=resolved_state_dir, step_index=step_index, step=step)
    if state.get("stage") == STAGE_DECISION_PENDING:
        return OrchestratorRunnerResult(state, ran_step=False)
    return OrchestratorRunnerResult(state, ran_step=False)
