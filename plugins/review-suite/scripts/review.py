#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any

from review_suite_runtime_bootstrap import bootstrap_from_installed_cache

bootstrap_from_installed_cache(__file__)

from review_gate import (
    gate_record_status,
    gate_signoff_decision_for_round,
    load_gate_record,
    record_gate_signoff_decision,
)
from review_suite_core import (
    AxiArgumentParser,
    current_branch,
    current_head,
    cwd_path_from_normalized,
    emit_error,
    emit_toon,
    format_command,
    has_worktree_changes,
    merge_base,
    record_review_anchor,
    resolve_repo_root,
    write_text,
)
from review_suite_core.config import default_state_dir, load_config
from review_suite_core.orchestrator_profiles import RESTART_MODE_ORDER, SUPPORTED_MODES, resolve_orchestrator_profile
from review_suite_core.orchestrator_runner import run_one_expensive_step
from review_suite_core.orchestrator_state import (
    DECISION_CLEAN,
    DECISION_FINDINGS,
    GITHUB_RESULT_CLEAN,
    GITHUB_RESULT_FINDINGS,
    GITHUB_RESULT_WAIVED,
    GITHUB_RESULT_COMMANDS,
    STAGE_CREATED,
    STAGE_DECISION_PENDING,
    STAGE_FIX_PENDING,
    STAGE_FOLLOWUP_PENDING,
    STAGE_GATE_RERUN_NEEDED,
    STAGE_LOCAL_GREEN_HANDOFF,
    STAGE_RETRY_REQUESTED,
    STAGE_REVIEW_GREEN,
    STAGE_ABORTED,
    DESLOP_STATUS_CLOSED,
    create_cycle,
    mark_deslop_closed,
    mark_fix_detected,
    record_clean_decision,
    record_findings_decision,
    record_followup_clean,
    record_followup_findings,
    record_github_result,
    record_validation_statuses,
    abort_cycle,
)
from review_suite_core.orchestrator_store import (
    load_cycle_by_key,
    load_cycle_by_public_id,
    register_cycle_state_dir,
    save_cycle,
    state_dir_for_public_id,
)
from review_suite_local import load_round, print_reviewer_output_section, public_task_name, round_needs_caller_grade


FOLLOWUP_LANE = "review-followup"
GATE_LANES = {"review_t2", "review_t4"}
CLI_VALIDATION_STATUSES = ("passed", "failed", "pending", "waived", "classified")
VALIDATION_READY_STATUSES = {"passed", "waived", "classified"}


def build_parser() -> argparse.ArgumentParser:
    parser = AxiArgumentParser(description="Run the review-suite orchestrator shell.")
    parser.add_argument("--id")
    parser.add_argument("--mode", choices=SUPPORTED_MODES)
    parser.add_argument("--restart-mode", choices=tuple(RESTART_MODE_ORDER))
    parser.add_argument("--reason")
    parser.add_argument("--cd")
    parser.add_argument("--base")
    parser.add_argument("--decision", choices=(DECISION_CLEAN, DECISION_FINDINGS))
    parser.add_argument("--github-review", action="store_true")
    parser.add_argument("--github-force", action="store_true")
    parser.add_argument("--github-result", choices=tuple(sorted(GITHUB_RESULT_COMMANDS)))
    parser.add_argument("--github-note")
    parser.add_argument("--focused-validation", choices=CLI_VALIDATION_STATUSES)
    parser.add_argument("--full-suite", choices=CLI_VALIDATION_STATUSES)
    parser.add_argument("--ci", choices=CLI_VALIDATION_STATUSES)
    parser.add_argument("--deslop-done", action="store_true")
    parser.add_argument("--show-findings", action="store_true", help="Print stored reviewer output for --id without running review.")
    parser.add_argument("--fresh-token", help=argparse.SUPPRESS)
    parser.add_argument("--state-dir", default=str(default_state_dir()), help=argparse.SUPPRESS)
    return parser


def _help_command() -> str:
    return format_command([sys.executable, str(Path(__file__).resolve()), "--help"])


def _review_command(public_id: str, *extra: str) -> str:
    return format_command([sys.executable, str(Path(__file__).resolve()), "--id", public_id, *extra])


def _new_review_command(state: dict[str, Any], *, state_dir: Path, fresh_token: str | None = None) -> str:
    identity = dict(state.get("identity") or {})
    mode = str(dict(state.get("mode") or {}).get("requested") or dict(state.get("mode") or {}).get("effective") or "").strip()
    cwd = str(identity.get("cwd") or "").strip()
    base = str(identity.get("base") or "").strip()
    if not mode or not cwd or not base:
        raise ValueError("review cycle is missing mode, cwd, or base for a fresh review command")
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--mode",
        mode,
        "--cd",
        str(cwd_path_from_normalized(cwd)),
        "--base",
        base,
        "--state-dir",
        str(state_dir),
    ]
    if fresh_token:
        command.extend(["--fresh-token", fresh_token])
    return format_command(command)


def _github_review_action_command(public_id: str) -> str:
    return _review_command(public_id, "--github-review")


def _github_result_command(public_id: str, result: str, *extra: str) -> str:
    return _review_command(public_id, "--github-result", result, *extra)


def _deslop_done_command(public_id: str) -> str:
    return _review_command(public_id, "--deslop-done")


def _arena_grade_command(state: dict[str, Any], *, state_dir: Path) -> str | None:
    pending_payload = _pending_grade_payload(state, state_dir=state_dir)
    if pending_payload is None:
        return None
    round_id = str(pending_payload.get("round_id") or "").strip()
    branch = str(dict(state.get("identity") or {}).get("branch") or "").strip()
    task_id = str(pending_payload.get("task_id_hint") or "").strip() or branch or str(state.get("public_id") or "").strip()
    return format_command(
        [
            sys.executable,
            str(Path(__file__).resolve().with_name("review_suite_arena.py")),
            "grade",
            "--round-id",
            round_id,
            "--task-id",
            task_id,
            "--winner",
            "WINNER",
            "--basis",
            "BASIS",
            "--state-dir",
            str(state_dir),
        ]
    )


def _arena_reroll_command(state: dict[str, Any], *, state_dir: Path, round_id: str, slot: str) -> str:
    identity = dict(state.get("identity") or {})
    command = [
        sys.executable,
        str(Path(__file__).resolve().with_name("review_suite_arena.py")),
        "reroll-slot",
        "--round-id",
        round_id,
        "--slot",
        slot,
        "--state-dir",
        str(state_dir),
        "--sqlite-path",
        str(Path.home() / ".codex" / "state_5.sqlite"),
    ]
    cwd = str(identity.get("cwd") or "").strip()
    if cwd:
        command.extend(["--cd", str(cwd_path_from_normalized(cwd))])
    base = str(identity.get("base") or "").strip()
    if base:
        command.extend(["--base", base])
    return format_command(command)


def _arena_dismiss_command(*, state_dir: Path, round_id: str) -> str:
    return format_command(
        [
            sys.executable,
            str(Path(__file__).resolve().with_name("review_suite_arena.py")),
            "dismiss-round",
            "--round-id",
            round_id,
            "--state-dir",
            str(state_dir),
            "--reason",
            "orchestrator_arena_blocked",
        ]
    )


def _arena_resume_command(*, state_dir: Path, round_id: str) -> str:
    return format_command(
        [
            sys.executable,
            str(Path(__file__).resolve().with_name("review_suite_arena.py")),
            "resume-round",
            "--round-id",
            round_id,
            "--state-dir",
            str(state_dir),
            "--sqlite-path",
            str(Path.home() / ".codex" / "state_5.sqlite"),
        ]
    )


def _arena_run_round_command(state: dict[str, Any], payload: dict[str, Any], *, state_dir: Path, round_id: str) -> str:
    identity = dict(state.get("identity") or {})
    review_scope = dict(payload.get("review_scope") or {})
    base = str(review_scope.get("base") or identity.get("base") or "").strip()
    command = [
        sys.executable,
        str(Path(__file__).resolve().with_name("review_suite_arena.py")),
        "run-round",
        "--round-id",
        round_id,
        "--base",
        base or "main",
        "--state-dir",
        str(state_dir),
        "--sqlite-path",
        str(Path.home() / ".codex" / "state_5.sqlite"),
    ]
    cwd = str(identity.get("cwd") or payload.get("review_cwd") or "").strip()
    if cwd:
        command.extend(["--cd", str(cwd_path_from_normalized(cwd))])
    return format_command(command)


def _arena_blocked_slots(payload: dict[str, Any]) -> list[str]:
    slots: list[str] = []
    for run in list(payload.get("runs") or []):
        if not isinstance(run, dict):
            continue
        if bool(run.get("blocked")) or bool(run.get("grade_blocked")):
            slot = str(run.get("slot") or "").strip()
            if slot:
                slots.append(slot)
    return slots


def _arena_recovery_action(state: dict[str, Any], *, state_dir: Path, public_id: str) -> dict[str, Any] | None:
    pending = dict(state.get("pending_action") or {})
    if state.get("stage") != STAGE_RETRY_REQUESTED or str(pending.get("kind") or "") != "arena-blocked":
        return None
    round_id = str(pending.get("round_id") or "").strip()
    if not round_id:
        return {"cmd": _review_command(public_id), "note": "Arena recovery is missing round id."}
    payload = _load_output_round_payload(state_dir, _round_by_id(state, round_id))
    next_cmd = _review_command(public_id)
    slots = _arena_blocked_slots(payload)
    if slots:
        reroll = {slot: _arena_reroll_command(state, state_dir=state_dir, round_id=round_id, slot=slot) for slot in slots}
        return {
            "cmd": reroll[slots[0]],
            "reroll": reroll,
            "dismiss": _arena_dismiss_command(state_dir=state_dir, round_id=round_id),
            "next": next_cmd,
            "note": "Reroll blocked arena slot(s), then rerun this review id. Dismiss reruns the arena step.",
        }
    status = str(payload.get("status") or "").strip()
    if status == "sampled":
        return {
            "cmd": _arena_run_round_command(state, payload, state_dir=state_dir, round_id=round_id),
            "dismiss": _arena_dismiss_command(state_dir=state_dir, round_id=round_id),
            "next": next_cmd,
            "note": "Run the arena replacement round, then rerun this review id.",
        }
    if status == "running":
        return {
            "cmd": _arena_resume_command(state_dir=state_dir, round_id=round_id),
            "dismiss": _arena_dismiss_command(state_dir=state_dir, round_id=round_id),
            "next": next_cmd,
            "note": "Resume the arena replacement round, then rerun this review id.",
        }
    if status and status not in {"completed", "dismissed"}:
        return {
            "cmd": _arena_dismiss_command(state_dir=state_dir, round_id=round_id),
            "next": next_cmd,
            "note": f"Arena replacement round is {status}; dismiss it, then rerun this review id.",
        }
    return {
        "cmd": next_cmd,
        "dismiss": _arena_dismiss_command(state_dir=state_dir, round_id=round_id),
        "note": "Arena recovery is ready; rerun this review id.",
    }


def _pending_grade_payload(state: dict[str, Any], *, state_dir: Path) -> dict[str, Any] | None:
    pending = dict(state.get("pending_action") or {})
    round_id = str(pending.get("round_id") or "").strip()
    if str(pending.get("kind") or "") != "decision" or not round_id:
        return None
    round_record = _round_record_by_id(state, round_id)
    if not bool(round_record.get("grading_required")):
        return None
    payload = _load_output_round_payload(state_dir, round_record)
    if not bool(round_record.get("arena_round") or payload.get("arena_round")):
        return None
    if not round_needs_caller_grade(payload):
        return None
    return payload


def _configured_selection(config: dict[str, Any]) -> str:
    return str(((config.get("orchestrator") or {}).get("selection") or "auto")).strip()


def _path_key(path: Path) -> str:
    key = str(path.resolve(strict=False))
    return key.lower() if sys.platform == "win32" else key


def _append_unique_path(paths: list[Path], path: Path | None) -> None:
    if path is None:
        return
    key = _path_key(path)
    if all(_path_key(item) != key for item in paths):
        paths.append(path)


def _legacy_state_dir_candidates() -> list[Path]:
    root = Path.home() / ".codex" / "review-suite-state"
    if not root.exists():
        return []
    try:
        return [item for item in sorted(root.iterdir()) if item.is_dir()]
    except OSError:
        return []


def _cycle_candidates(initial_state_dir: Path, public_id: str) -> list[Path]:
    candidates: list[Path] = []
    default_dir = Path(default_state_dir())
    _append_unique_path(candidates, initial_state_dir)
    _append_unique_path(candidates, state_dir_for_public_id(default_dir, public_id))
    _append_unique_path(candidates, default_dir)
    for candidate in _legacy_state_dir_candidates():
        _append_unique_path(candidates, candidate)
    return candidates


def _load_cycle_and_state_dir(initial_state_dir: Path, public_id: str) -> tuple[Path, dict[str, Any]]:
    for candidate in _cycle_candidates(initial_state_dir, public_id):
        try:
            return candidate, load_cycle_by_public_id(candidate, public_id)
        except ValueError:
            continue
    raise ValueError(f"unknown review cycle id: {public_id}")


def _register_saved_cycle(state_dir: Path, state: dict[str, Any]) -> None:
    register_cycle_state_dir(
        locator_state_dir=Path(default_state_dir()),
        state_dir=state_dir,
        public_id=str(state.get("public_id") or ""),
        cycle_key=str(state.get("cycle_key") or ""),
    )


def _base_arg(args: argparse.Namespace) -> str:
    return str(args.base or "main")


def _reject_id_creation_args(args: argparse.Namespace, state: dict[str, Any]) -> None:
    sent = [name for name in ("mode", "cd", "base") if getattr(args, name) is not None]
    if not sent:
        return
    mode = dict(state.get("mode") or {})
    identity = dict(state.get("identity") or {})
    context = []
    locked_mode = str(mode.get("effective") or mode.get("requested") or "").strip()
    if locked_mode:
        context.append(f"mode {locked_mode}")
    cwd = str(identity.get("cwd") or "").strip()
    if cwd:
        context.append(cwd)
    suffix = f"; this id is locked to {' at '.join(context)}" if context else ""
    raise ValueError(f"--id already selects review context; remove --{', --'.join(sent)}{suffix}")


def _restart_reason(args: argparse.Namespace) -> str:
    reason = str(args.reason or "").strip()
    if not reason or reason == "REASON":
        raise ValueError("--reason is required for --restart-mode")
    return reason


def _mode_rank(mode: str) -> int:
    if mode not in RESTART_MODE_ORDER:
        allowed = ", ".join(RESTART_MODE_ORDER)
        raise ValueError(f"review cycle mode {mode} cannot be restarted; supported restart modes: {allowed}")
    return RESTART_MODE_ORDER[mode]


def _validate_restart_mode(state: dict[str, Any], target_mode: str) -> str:
    mode = dict(state.get("mode") or {})
    current_mode = str(mode.get("effective") or mode.get("requested") or "").strip()
    current_rank = _mode_rank(current_mode)
    target_rank = _mode_rank(target_mode)
    if target_rank <= current_rank:
        raise ValueError(f"--restart-mode must increase strictness from {current_mode}; requested {target_mode}")
    return current_mode


def _normalized_branch(branch: str | None) -> str | None:
    value = str(branch or "").strip()
    return None if value in {"", "HEAD"} else value


def _round_by_id(state: dict[str, Any], round_id: str) -> dict[str, Any]:
    for item in list(state.get("rounds") or []):
        if isinstance(item, dict) and item.get("round_id") == round_id:
            return dict(item)
    return {}


def _pending_decision(state: dict[str, Any]) -> tuple[str, str]:
    pending = dict(state.get("pending_action") or {})
    round_id = str(pending.get("round_id") or "").strip()
    lane = str(pending.get("lane") or "").strip()
    if round_id and lane:
        return round_id, lane
    for item in reversed(list(state.get("rounds") or [])):
        if not isinstance(item, dict) or item.get("status") != STAGE_DECISION_PENDING:
            continue
        round_id = str(item.get("round_id") or "").strip()
        lane = str(item.get("lane") or "").strip()
        if round_id and lane:
            return round_id, lane
    raise ValueError("no decision is pending for this review cycle")


def _with_fix_action(state: dict[str, Any]) -> dict[str, Any]:
    next_state = dict(state)
    active = dict(next_state.get("active_findings") or {})
    next_state["pending_action"] = {
        "kind": "fix-findings",
        "round_id": active.get("round_id"),
    }
    return next_state


def _round_gate(round_payload: dict[str, Any], lane: str) -> str | None:
    gate = str(round_payload.get("gate") or "").strip()
    if gate:
        return gate
    return lane if lane in GATE_LANES else None


def _gate_output_refs(runs: list[object]) -> list[str]:
    refs: list[str] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        ref = str(run.get("reviewer_output_ref") or "").strip()
        if ref:
            refs.append(ref)
    return refs


def _orchestrator_review_state_dir(state_dir: Path) -> Path:
    return state_dir / "orchestrator" / "review-rounds"


def _unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        resolved = path.resolve(strict=False)
        key = str(resolved).lower() if sys.platform == "win32" else str(resolved)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _round_state_dir_candidates(state_dir: Path, round_record: dict[str, Any]) -> list[Path]:
    candidates: list[Path] = []
    round_state_dir = str(round_record.get("round_state_dir") or "").strip()
    if round_state_dir:
        candidates.append(Path(round_state_dir))
    candidates.extend([_orchestrator_review_state_dir(state_dir), state_dir])
    return _unique_paths(candidates)


def _task_class_for_lane(lane: str) -> str:
    return {
        "review_t1": "phase_review",
        "review_t2": "phase_gate",
        "review_t3": "pr_review",
        "review_t4": "pr_gate",
        FOLLOWUP_LANE: "phase_review",
    }.get(lane, lane)


def _fallback_round_payload(round_record: dict[str, Any]) -> dict[str, Any]:
    lane = str(round_record.get("lane") or "").strip()
    payload = dict(round_record)
    payload["task_class"] = str(payload.get("task_class") or _task_class_for_lane(lane))
    payload["status"] = str(payload.get("status") or payload.get("review_status") or "unknown")
    runs = []
    for raw_run in list(payload.get("runs") or []):
        if not isinstance(raw_run, dict):
            continue
        run = dict(raw_run)
        if not str(run.get("review_status") or "").strip() and str(run.get("status") or "").strip():
            run["review_status"] = run.get("status")
        if not str(run.get("reviewer_output") or "").strip() and str(run.get("summary") or "").strip():
            run["reviewer_output"] = run.get("summary")
        if not str(run.get("reviewer_output_ref") or "").strip() and str(run.get("ref") or "").strip():
            run["reviewer_output_ref"] = run.get("ref")
        runs.append(run)
    payload["runs"] = runs
    return payload


def _round_record_by_id(state: dict[str, Any], round_id: str) -> dict[str, Any]:
    for item in list(state.get("rounds") or []):
        if isinstance(item, dict) and str(item.get("round_id") or "") == round_id:
            return dict(item)
    return {}


def _load_output_round_payload(state_dir: Path, round_record: dict[str, Any]) -> dict[str, Any]:
    round_id = str(round_record.get("round_id") or "").strip()
    if not round_id:
        return _fallback_round_payload(round_record)
    for candidate in _round_state_dir_candidates(state_dir, round_record):
        try:
            return load_round(candidate, round_id)
        except ValueError:
            continue
    gate_record = load_gate_record(state_dir, round_id)
    if gate_record is not None:
        return gate_record
    return _fallback_round_payload(round_record)


def _payload_has_reviewer_output(payload: dict[str, Any]) -> bool:
    for run in list(payload.get("runs") or []):
        if not isinstance(run, dict):
            continue
        for key in ("reviewer_output", "status_summary", "summary"):
            if str(run.get(key) or "").strip():
                return True
    return False


def _output_round_candidates(state: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_round_id(round_id: str) -> None:
        value = str(round_id or "").strip()
        if not value or value in seen:
            return
        seen.add(value)
        record = _round_record_by_id(state, value)
        if record:
            candidates.append(record)
        else:
            candidates.append({"round_id": value})

    pending = dict(state.get("pending_action") or {})
    if str(pending.get("kind") or "") in {"collect-review-step", "decision"}:
        add_round_id(str(pending.get("round_id") or ""))
    active = state.get("active_findings")
    if isinstance(active, dict):
        add_round_id(str(active.get("round_id") or ""))
    for item in reversed(list(state.get("rounds") or [])):
        if isinstance(item, dict):
            add_round_id(str(item.get("round_id") or ""))
    return candidates


def _show_findings(state: dict[str, Any], *, state_dir: Path) -> int:
    public_id = str(state.get("public_id") or "").strip()
    candidates = _output_round_candidates(state)
    if not candidates:
        write_text(f"review: {public_id}")
        write_text("no stored reviewer output found")
        return 0
    selected = candidates[0]
    payload = _load_output_round_payload(state_dir, selected)
    for candidate in candidates:
        candidate_payload = _load_output_round_payload(state_dir, candidate)
        if _payload_has_reviewer_output(candidate_payload):
            selected = candidate
            payload = candidate_payload
            break
    round_id = str(payload.get("round_id") or selected.get("round_id") or "").strip()
    lane = str(selected.get("lane") or payload.get("public_task") or "").strip()
    task = public_task_name(str(payload.get("task_class") or ""))
    write_text(f"review: {public_id}")
    write_text(f"round_id: {round_id}")
    if lane:
        write_text(f"lane: {lane}")
    if task:
        write_text(f"task: {task}")
    status = str(payload.get("status") or selected.get("status") or "").strip()
    if status:
        write_text(f"status: {status}")
    write_text("")
    if not print_reviewer_output_section([run for run in list(payload.get("runs") or []) if isinstance(run, dict)]):
        write_text("no stored reviewer output found for this round")
    return 0


def _record_gate_decision(
    *,
    state_dir: Path,
    round_id: str,
    lane: str,
    verdict: str,
) -> None:
    gate_record = load_gate_record(state_dir, round_id)
    if gate_record is None:
        raise ValueError(f"gate round not found: {round_id}")
    existing = gate_signoff_decision_for_round(state_dir, round_id)
    status = gate_record_status(gate_record, existing)
    if status == "blocked":
        raise ValueError(f"blocked gate rounds cannot be closed as signoff decisions: {round_id}")
    if existing:
        record_gate_signoff_decision(
            state_dir=state_dir,
            gate_record=gate_record,
            verdict=verdict,
            workflow_anchor_recorded=bool(existing.get("workflow_anchor_recorded")),
        )
        return
    workflow_anchor_recorded = False
    review_cwd_text = str(gate_record.get("review_cwd") or "").strip()
    if verdict == DECISION_CLEAN:
        if not review_cwd_text:
            raise ValueError(f"gate round is missing review_cwd and cannot be anchored: {round_id}")
        review_scope = dict(gate_record.get("review_scope") or {})
        record_review_anchor(
            state_dir=state_dir,
            review_cwd=Path(review_cwd_text),
            lane=lane,
            base=str(review_scope.get("base") or "") or None,
            review_scope=review_scope,
            round_id=round_id,
            task_id=str(gate_record.get("task_id") or round_id),
            output_refs=_gate_output_refs(list(gate_record.get("runs") or [])),
        )
        workflow_anchor_recorded = True
    record_gate_signoff_decision(
        state_dir=state_dir,
        gate_record=gate_record,
        verdict=verdict,
        workflow_anchor_recorded=workflow_anchor_recorded,
    )


def _identity_head(state: dict[str, Any]) -> str | None:
    identity = dict(state.get("identity") or {})
    cwd = str(identity.get("cwd") or "").strip()
    if not cwd:
        return None
    try:
        return current_head(cwd_path_from_normalized(cwd))
    except (OSError, ValueError):
        return None


def _resume_progress(state: dict[str, Any]) -> dict[str, Any]:
    stage = state.get("stage")
    if stage in {STAGE_CREATED, STAGE_FOLLOWUP_PENDING}:
        return state
    if stage != STAGE_FIX_PENDING:
        return state

    active = dict(state.get("active_findings") or {})
    reviewed_head = str(active.get("reviewed_head") or "").strip()
    head = _identity_head(state)
    if head and reviewed_head and head != reviewed_head:
        return mark_fix_detected(state, head=head)
    return _with_fix_action(state)


def _apply_profile_resolution(state: dict[str, Any], resolution: Any) -> dict[str, Any]:
    state["selection"]["reason"] = resolution.selection_reason
    state["grading"] = {"required": bool(resolution.requires_grading)}

    def step_payload(step: Any) -> dict[str, Any]:
        payload = {
            "kind": step.kind,
            "name": step.name,
        }
        if step.kind == "gate":
            payload["gate"] = step.gate
            return payload
        if step.kind == "arena":
            payload["lane"] = step.lane
            payload["task_class"] = step.task_class
            return payload
        payload.update(
            {
                "count": step.count,
                "model": step.model,
                "reasoning_effort": step.reasoning_effort,
                "service_tier": step.service_tier,
            }
        )
        if step.rerun_on_findings:
            payload["rerun_on_findings"] = True
        if step.max_review_rounds is not None:
            payload["max_review_rounds"] = step.max_review_rounds
        return payload

    state["review_plan"] = {
        "steps": [step_payload(step) for step in resolution.steps],
    }
    return state


def _create_or_resume_cycle(*, args: argparse.Namespace, state_dir: Path) -> dict[str, Any]:
    if not args.mode:
        raise ValueError("--mode is required when creating a review cycle")
    review_root = resolve_repo_root(args.cd)
    head = current_head(review_root)
    branch = current_branch(review_root)
    base = _base_arg(args)
    merge_base_head = merge_base(review_root, base, "HEAD")
    config = load_config(state_dir)
    resolution = resolve_orchestrator_profile(config, mode=str(args.mode), selection=_configured_selection(config))
    state = create_cycle(
        cwd=review_root,
        base=base,
        branch=branch,
        head=head,
        merge_base=merge_base_head,
        requested_mode=resolution.requested_mode,
        effective_mode=resolution.effective_mode,
        selection=resolution.requested_selection,
        effective_selection=resolution.effective_selection,
        deslop_enabled=resolution.profile.deslop_enabled,
        cycle_token=str(args.fresh_token or "").strip() or None,
    )
    existing = load_cycle_by_key(state_dir, str(state["cycle_key"]))
    if existing is not None:
        return existing
    return _apply_profile_resolution(state, resolution)


def _current_restart_identity(state: dict[str, Any]) -> tuple[Path, str, str | None, str, str]:
    identity = dict(state.get("identity") or {})
    cwd = str(identity.get("cwd") or "").strip()
    if not cwd:
        raise ValueError("review cycle is missing cwd and cannot be restarted")
    base = str(identity.get("base") or "").strip()
    if not base:
        raise ValueError("review cycle is missing base and cannot be restarted")
    review_root = cwd_path_from_normalized(cwd)
    old_branch = _normalized_branch(str(identity.get("branch") or ""))
    branch = _normalized_branch(current_branch(review_root))
    if branch != old_branch:
        raise ValueError(f"cannot restart review cycle on branch {branch or 'HEAD'}; expected {old_branch or 'HEAD'}")
    if has_worktree_changes(review_root):
        raise ValueError("cannot restart review cycle with a dirty worktree; commit or stash changes, then rerun")
    head = current_head(review_root)
    expected_head = str(identity.get("head") or "").strip()
    if head != expected_head:
        raise ValueError("cannot restart review cycle after HEAD changed; start a new review instead")
    merge_base_head = merge_base(review_root, base, "HEAD")
    expected_merge_base = str(identity.get("merge_base") or "").strip()
    if merge_base_head != expected_merge_base:
        raise ValueError("cannot restart review cycle after merge-base changed; start a new review instead")
    return review_root, base, branch, head, merge_base_head


def _create_restart_cycle(
    *,
    state: dict[str, Any],
    state_dir: Path,
    target_mode: str,
    reason: str,
) -> tuple[dict[str, Any], bool]:
    if isinstance(state.get("superseded_by"), dict):
        replacement = str(dict(state.get("superseded_by") or {}).get("review") or "").strip()
        raise ValueError(f"review cycle is already superseded{f' by {replacement}' if replacement else ''}")
    current_mode = _validate_restart_mode(state, target_mode)
    review_root, base, branch, head, merge_base_head = _current_restart_identity(state)
    config = load_config(state_dir)
    selection = str(dict(state.get("selection") or {}).get("requested") or _configured_selection(config)).strip()
    resolution = resolve_orchestrator_profile(config, mode=target_mode, selection=selection)
    restart_token = f"{state.get('cycle_key')}:{target_mode}"
    replacement = create_cycle(
        cwd=review_root,
        base=base,
        branch=branch,
        head=head,
        merge_base=merge_base_head,
        requested_mode=resolution.requested_mode,
        effective_mode=resolution.effective_mode,
        selection=resolution.requested_selection,
        effective_selection=resolution.effective_selection,
        deslop_enabled=resolution.profile.deslop_enabled,
        restart_token=restart_token,
    )
    existing = load_cycle_by_key(state_dir, str(replacement["cycle_key"]))
    if existing is not None:
        return existing, True
    replacement["restart"].update(
        {
            "supersedes": str(state.get("public_id") or ""),
            "supersedes_cycle_key": str(state.get("cycle_key") or ""),
            "from_mode": current_mode,
            "reason": reason,
        }
    )
    return _apply_profile_resolution(replacement, resolution), False


def _restart_cycle(state: dict[str, Any], *, state_dir: Path, target_mode: str, reason: str) -> dict[str, Any]:
    replacement, existing_replacement = _create_restart_cycle(
        state=state,
        state_dir=state_dir,
        target_mode=target_mode,
        reason=reason,
    )
    if not existing_replacement:
        replacement = _advance_without_decision(replacement, state_dir=state_dir)
    saved_replacement = save_cycle(state_dir, replacement)
    _register_saved_cycle(state_dir, saved_replacement)
    superseded = abort_cycle(state, reason=reason)
    superseded["superseded_by"] = {
        "review": str(saved_replacement.get("public_id") or ""),
        "cycle_key": str(saved_replacement.get("cycle_key") or ""),
        "mode": target_mode,
        "reason": reason,
    }
    saved_superseded = save_cycle(state_dir, superseded)
    _register_saved_cycle(state_dir, saved_superseded)
    return saved_replacement


def _advance_without_decision(state: dict[str, Any], *, state_dir: Path) -> dict[str, Any]:
    ready_state = _resume_progress(state)

    def persist_running(next_state: dict[str, Any]) -> dict[str, Any]:
        saved = save_cycle(state_dir, next_state)
        _register_saved_cycle(state_dir, saved)
        return saved

    result = run_one_expensive_step(ready_state, state_dir=state_dir, persist_state=persist_running)
    if result.ran_step:
        return result.state
    return _resume_progress(ready_state)


def _apply_decision(state: dict[str, Any], decision: str, *, state_dir: Path) -> dict[str, Any]:
    ready_state = _resume_progress(state)
    if ready_state.get("stage") != STAGE_DECISION_PENDING:
        raise ValueError("no decision is pending for this review cycle")
    round_id, lane = _pending_decision(ready_state)
    if _pending_grade_payload(ready_state, state_dir=state_dir) is not None:
        raise ValueError("grade the arena round before recording a clean/findings decision")
    round_payload = _round_by_id(ready_state, round_id)
    reviewed_head = str(round_payload.get("reviewed_head") or "").strip() or None
    gate = _round_gate(round_payload, lane)
    if decision == DECISION_CLEAN:
        if lane == FOLLOWUP_LANE:
            return record_followup_clean(ready_state, round_id=round_id, reviewed_head=reviewed_head)
        next_state = record_clean_decision(ready_state, round_id=round_id, lane=lane, reviewed_head=reviewed_head, gate=gate)
        if gate:
            _record_gate_decision(state_dir=state_dir, round_id=round_id, lane=lane, verdict=decision)
        return next_state
    if decision == DECISION_FINDINGS:
        if lane == FOLLOWUP_LANE:
            return _with_fix_action(record_followup_findings(ready_state, round_id=round_id, reviewed_head=reviewed_head))
        next_state = _with_fix_action(record_findings_decision(ready_state, round_id=round_id, lane=lane, reviewed_head=reviewed_head, gate=gate))
        if gate:
            _record_gate_decision(state_dir=state_dir, round_id=round_id, lane=lane, verdict=decision)
        return next_state
    raise ValueError(f"unsupported decision: {decision}")


def _has_validation_status(args: argparse.Namespace) -> bool:
    return any(
        getattr(args, name) is not None
        for name in ("focused_validation", "full_suite", "ci")
    )


def _record_validation_status(state: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    return record_validation_statuses(
        state,
        focused=args.focused_validation,
        full_suite=args.full_suite,
        ci=args.ci,
    )


def _fresh_review_token(state: dict[str, Any]) -> str:
    pending = dict(state.get("pending_action") or {})
    active = dict(state.get("active_findings") or {})
    parts = [
        "budget-exhausted",
        str(state.get("cycle_key") or "").strip(),
        str(pending.get("round_id") or active.get("round_id") or "").strip(),
        str(active.get("fix_head") or "").strip(),
    ]
    return ":".join(part for part in parts if part)


def _record_github_result(state: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    return record_github_result(
        state,
        result=str(args.github_result),
        note=args.github_note,
    )


def _validation_blockers(state: dict[str, Any]) -> list[str]:
    validation = dict(state.get("validation") or {})
    blockers: list[str] = []
    for key in ("full_suite", "ci"):
        value = str(validation.get(key) or "unknown").strip() or "unknown"
        if value not in VALIDATION_READY_STATUSES:
            blockers.append(f"{key}:{value}")
    return blockers


def _last_decision_is_clean_followup(state: dict[str, Any]) -> bool:
    for item in reversed(list(state.get("decisions") or [])):
        if not isinstance(item, dict):
            continue
        return (
            str(item.get("lane") or "") == FOLLOWUP_LANE
            and str(item.get("command") or "") == DECISION_CLEAN
        )
    return False


def _review_step_label(state: dict[str, Any], pending: dict[str, Any]) -> str:
    step = str(pending.get("step") or "").strip() or "next review step"
    try:
        position = int(pending.get("step_index")) + 1
    except (TypeError, ValueError):
        position = None
    steps = list(dict(state.get("review_plan") or {}).get("steps") or [])
    if position is not None and steps:
        return f"review step {position}/{len(steps)} {step}"
    return f"review step {step}"


def _continuation_note(state: dict[str, Any]) -> str | None:
    pending = dict(state.get("pending_action") or {})
    kind = str(pending.get("kind") or "").strip()
    if kind == "run-review-step":
        if not _last_decision_is_clean_followup(state):
            return None
        label = _review_step_label(state, pending)
        return f"Clean follow-up is not final signoff; run {label} before treating the review as green."
    if kind == "rerun-gate":
        if not _last_decision_is_clean_followup(state):
            return None
        gate = str(pending.get("gate") or pending.get("lane") or "").strip() or "the same gate"
        return f"Clean follow-up is not final signoff; rerun {gate} before treating the review as green."
    return None


def _github_handoff_action(state: dict[str, Any], *, state_dir: Path) -> dict[str, Any]:
    public_id = str(state.get("public_id") or "").strip()
    action: dict[str, Any] = {
        "cmd": _github_review_action_command(public_id),
        "after": "PR create/update",
        "result": {
            GITHUB_RESULT_CLEAN: _github_result_command(public_id, GITHUB_RESULT_CLEAN),
            GITHUB_RESULT_FINDINGS: _github_result_command(public_id, GITHUB_RESULT_FINDINGS),
            GITHUB_RESULT_WAIVED: _github_result_command(public_id, GITHUB_RESULT_WAIVED, "--github-note", "REASON"),
        },
    }
    github_review = dict(state.get("github_review") or {})
    if str(github_review.get("status") or "") == GITHUB_RESULT_FINDINGS:
        action["note"] = "GitHub findings were fixed and locally signed off; request GitHub review again."
    blockers = _validation_blockers(state)
    if blockers:
        action["blocked_by"] = blockers
    return action


def _validation_status_command(public_id: str, blockers: list[str], status: str) -> str:
    args: list[str] = []
    for blocker in blockers:
        key = blocker.split(":", 1)[0]
        if key == "full_suite":
            args.extend(["--full-suite", status])
        if key == "ci":
            args.extend(["--ci", status])
    return _review_command(public_id, *args)


def _validation_blocker_action(public_id: str, blockers: list[str]) -> dict[str, Any]:
    return {
        "cmd": _validation_status_command(public_id, blockers, "passed"),
        "alt": _validation_status_command(public_id, blockers, "waived"),
        "blocked_by": blockers,
        "note": "GitHub result is recorded; record full-suite/CI before PR-final or merge-ready.",
    }


def _github_review_is_terminal(state: dict[str, Any]) -> bool:
    github_review = dict(state.get("github_review") or {})
    status = str(github_review.get("status") or "").strip()
    if status not in {GITHUB_RESULT_CLEAN, GITHUB_RESULT_WAIVED}:
        return False
    reviewed_head = str(github_review.get("reviewed_head") or "").strip()
    if not reviewed_head:
        return False
    current_head_value = _identity_head(state)
    if not current_head_value:
        current_head_value = str(dict(state.get("review_heads") or {}).get("last_reviewed_head") or "").strip()
    if not current_head_value:
        current_head_value = str(dict(state.get("identity") or {}).get("head") or "").strip()
    return bool(current_head_value) and reviewed_head == current_head_value


def _github_terminal_action(state: dict[str, Any], public_id: str) -> dict[str, Any] | None:
    blockers = _validation_blockers(state)
    if blockers:
        return _validation_blocker_action(public_id, blockers)
    return None


def _deslop_is_open(state: dict[str, Any]) -> bool:
    deslop = dict(state.get("deslop") or {})
    if not bool(deslop.get("tracked")):
        return False
    return str(deslop.get("status") or "").strip() != DESLOP_STATUS_CLOSED


def _with_deslop_done_action(state: dict[str, Any], action: dict[str, Any] | None, public_id: str) -> dict[str, Any] | None:
    if not _deslop_is_open(state):
        return action
    if action is None:
        return {"cmd": _deslop_done_command(public_id)}
    next_action = dict(action or {})
    next_action["deslop_done"] = _deslop_done_command(public_id)
    return next_action


def _action_payload(state: dict[str, Any], *, state_dir: Path) -> dict[str, Any] | None:
    public_id = str(state.get("public_id") or "").strip()
    action: dict[str, Any] | None
    stage = state.get("stage")
    if stage == STAGE_ABORTED and isinstance(state.get("superseded_by"), dict):
        replacement = dict(state.get("superseded_by") or {})
        replacement_id = str(replacement.get("review") or "").strip()
        if replacement_id:
            action = {
                "cmd": _review_command(replacement_id),
                "note": f"Review {public_id} was superseded by {replacement_id}.",
            }
            return _with_deslop_done_action(state, action, public_id)
    arena_recovery = _arena_recovery_action(state, state_dir=state_dir, public_id=public_id)
    if arena_recovery:
        return _with_deslop_done_action(state, arena_recovery, public_id)
    if stage == STAGE_DECISION_PENDING:
        grade = _arena_grade_command(state, state_dir=state_dir)
        if grade:
            action = {
                "cmd": grade,
                "note": "Grade the arena round, then rerun this review id to choose clean or findings.",
                "next": _review_command(public_id),
            }
            return _with_deslop_done_action(state, action, public_id)
        action = {
            "cmd": _review_command(public_id, "--decision", DECISION_CLEAN),
            "alt": _review_command(public_id, "--decision", DECISION_FINDINGS),
        }
        return _with_deslop_done_action(state, action, public_id)
    if stage == STAGE_FIX_PENDING:
        pending = dict(state.get("pending_action") or {})
        if pending.get("kind") == "review-round-budget-exhausted":
            max_rounds = pending.get("max_review_rounds")
            step = str(pending.get("step") or "review").strip() or "review"
            fresh_token = _fresh_review_token(state)
            action = {
                "cmd": _new_review_command(state, state_dir=state_dir, fresh_token=fresh_token),
                "note": (
                    f"{step} reached its {max_rounds} round review budget; "
                    "no more local reviewers will be launched. "
                    "Action.cmd starts a new review if the latest fix needs another local review pass."
                ),
            }
            return _with_deslop_done_action(state, action, public_id)
        action = {
            "cmd": _review_command(public_id),
            "note": "Fix valid findings, then rerun this command.",
        }
        return _with_deslop_done_action(state, action, public_id)
    if stage in {STAGE_CREATED, STAGE_GATE_RERUN_NEEDED}:
        action = {"cmd": _review_command(public_id)}
        note = _continuation_note(state)
        if note:
            action["note"] = note
        return _with_deslop_done_action(state, action, public_id)
    if stage in {STAGE_REVIEW_GREEN, STAGE_LOCAL_GREEN_HANDOFF}:
        if _github_review_is_terminal(state):
            action = _github_terminal_action(state, public_id)
        else:
            action = _github_handoff_action(state, state_dir=state_dir)
        return _with_deslop_done_action(state, action, public_id)
    return _with_deslop_done_action(state, {"cmd": _review_command(public_id)}, public_id)


def _render(state: dict[str, Any], *, state_dir: Path) -> None:
    payload: dict[str, Any] = {
        "review": state.get("public_id"),
    }
    grading = dict(state.get("grading") or {})
    if grading.get("required"):
        payload["grading"] = "required"
    action = _action_payload(state, state_dir=state_dir)
    if action:
        payload["Action"] = action
    github_review = dict(state.get("github_review") or {})
    github_status = str(github_review.get("status") or "").strip()
    if github_status and github_status != "unknown":
        payload["github_review"] = github_status
    emit_toon(payload)


def _require_local_green_for_github_review(state: dict[str, Any]) -> None:
    if state.get("stage") not in {STAGE_REVIEW_GREEN, STAGE_LOCAL_GREEN_HANDOFF}:
        raise ValueError("--github-review requires local green review state")


def _github_review_subprocess_command(state: dict[str, Any], *, state_dir: Path, force: bool) -> list[str]:
    identity = dict(state.get("identity") or {})
    cwd = str(identity.get("cwd") or "").strip()
    if not cwd:
        raise ValueError("review cycle is missing cwd for --github-review")
    command = [
        sys.executable,
        str(Path(__file__).with_name("review_github.py").resolve()),
        "run",
        "--cd",
        str(cwd_path_from_normalized(cwd)),
        "--state-dir",
        str(state_dir),
    ]
    if force:
        command.append("--force")
    return command


def _run_github_review(state: dict[str, Any], *, state_dir: Path, force: bool) -> int:
    _require_local_green_for_github_review(state)
    command = _github_review_subprocess_command(state, state_dir=state_dir, force=force)
    return subprocess.run(command, check=False).returncode


def main() -> int:
    parser = build_parser()
    try:
        args = parser.parse_args()
        state_dir = Path(args.state_dir)
        has_validation_status = _has_validation_status(args)
        if args.restart_mode and not args.id:
            raise ValueError("--restart-mode requires --id")
        if args.fresh_token and args.id:
            raise ValueError("--fresh-token cannot be combined with --id")
        if args.reason and not args.restart_mode:
            raise ValueError("--reason requires --restart-mode")
        if args.deslop_done and not args.id:
            raise ValueError("--deslop-done requires --id")
        if args.deslop_done and (
            args.restart_mode
            or args.decision
            or args.github_review
            or args.github_force
            or args.github_result
            or args.github_note
            or has_validation_status
            or args.show_findings
        ):
            raise ValueError(
                "--deslop-done cannot be combined with restart, decisions, GitHub review, GitHub results, "
                "GitHub notes, validation status flags, or show-findings"
            )
        if args.restart_mode and (
            args.decision or args.github_review or args.github_force or args.github_result or has_validation_status
        ):
            raise ValueError("--restart-mode cannot be combined with decisions, GitHub review, GitHub results, or validation status flags")
        if args.decision and not args.id:
            raise ValueError("--decision requires --id")
        if args.github_force and not args.github_review:
            raise ValueError("--github-force requires --github-review")
        if args.github_review and not args.id:
            raise ValueError("--github-review requires --id")
        if args.github_review and (args.decision or args.github_result or args.github_note or has_validation_status):
            raise ValueError("--github-review cannot be combined with decisions, GitHub results, GitHub notes, or validation status flags")
        if args.github_result and not args.id:
            raise ValueError("--github-result requires --id")
        if args.github_result and (args.decision or has_validation_status):
            raise ValueError("--github-result cannot be combined with decisions or validation status flags")
        if args.github_note and not args.github_result:
            raise ValueError("--github-note requires --github-result")
        if has_validation_status and not args.id:
            raise ValueError("validation status flags require --id")
        if args.show_findings and not args.id:
            raise ValueError("--show-findings requires --id")
        if args.show_findings and (
            args.restart_mode
            or args.decision
            or args.github_review
            or args.github_force
            or args.github_result
            or args.github_note
            or has_validation_status
        ):
            raise ValueError("--show-findings cannot be combined with decisions, GitHub review, GitHub results, validation status flags, or restart")
        if args.id:
            state_dir, state = _load_cycle_and_state_dir(state_dir, str(args.id))
            _reject_id_creation_args(args, state)
            if args.show_findings:
                return _show_findings(state, state_dir=state_dir)
            if args.deslop_done:
                state = mark_deslop_closed(state)
                saved = save_cycle(state_dir, state)
                _register_saved_cycle(state_dir, saved)
                _render(saved, state_dir=state_dir)
                return 0
            if args.restart_mode:
                state = _restart_cycle(
                    state,
                    state_dir=state_dir,
                    target_mode=str(args.restart_mode),
                    reason=_restart_reason(args),
                )
                _render(state, state_dir=state_dir)
                return 0
            if args.github_review:
                return _run_github_review(state, state_dir=state_dir, force=bool(args.github_force))
            if args.github_result:
                state = _record_github_result(state, args)
                saved = save_cycle(state_dir, state)
                _register_saved_cycle(state_dir, saved)
                _render(saved, state_dir=state_dir)
                return 0
            if args.decision:
                state = _apply_decision(state, str(args.decision), state_dir=state_dir)
            if has_validation_status:
                state = _record_validation_status(state, args)
            elif not args.decision:
                state = _advance_without_decision(state, state_dir=state_dir)
        else:
            state = _create_or_resume_cycle(args=args, state_dir=state_dir)
            state = _advance_without_decision(state, state_dir=state_dir)
        saved = save_cycle(state_dir, state)
        _register_saved_cycle(state_dir, saved)
        _render(saved, state_dir=state_dir)
        return 0
    except ValueError as exc:
        return emit_error(str(exc), status="usage_error", help_items=[_help_command()])


if __name__ == "__main__":
    raise SystemExit(main())
