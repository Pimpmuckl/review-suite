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
)
from review_suite_core.config import default_state_dir, load_config
from review_suite_core.orchestrator_profiles import RESTART_MODE_ORDER, SUPPORTED_MODES, resolve_orchestrator_profile
from review_suite_core.orchestrator_runner import run_one_expensive_step
from review_suite_core.orchestrator_state import (
    DECISION_CLEAN,
    DECISION_FINDINGS,
    STAGE_CREATED,
    STAGE_DECISION_PENDING,
    STAGE_FIX_PENDING,
    STAGE_FOLLOWUP_PENDING,
    STAGE_GATE_RERUN_NEEDED,
    STAGE_LOCAL_GREEN_HANDOFF,
    STAGE_REVIEW_GREEN,
    STAGE_ABORTED,
    create_cycle,
    mark_fix_detected,
    record_clean_decision,
    record_findings_decision,
    record_followup_clean,
    record_followup_findings,
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
    parser.add_argument("--focused-validation", choices=CLI_VALIDATION_STATUSES)
    parser.add_argument("--full-suite", choices=CLI_VALIDATION_STATUSES)
    parser.add_argument("--ci", choices=CLI_VALIDATION_STATUSES)
    parser.add_argument("--state-dir", default=str(default_state_dir()), help=argparse.SUPPRESS)
    return parser


def _help_command() -> str:
    return format_command([sys.executable, str(Path(__file__).resolve()), "--help"])


def _review_command(public_id: str, *extra: str) -> str:
    return format_command([sys.executable, str(Path(__file__).resolve()), "--id", public_id, *extra])


def _github_review_action_command(public_id: str) -> str:
    return _review_command(public_id, "--github-review")


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
    }
    blockers = _validation_blockers(state)
    if blockers:
        action["blocked_by"] = blockers
    return action


def _action_payload(state: dict[str, Any], *, state_dir: Path) -> dict[str, Any] | None:
    public_id = str(state.get("public_id") or "").strip()
    stage = state.get("stage")
    if stage == STAGE_ABORTED and isinstance(state.get("superseded_by"), dict):
        replacement = dict(state.get("superseded_by") or {})
        replacement_id = str(replacement.get("review") or "").strip()
        if replacement_id:
            return {
                "cmd": _review_command(replacement_id),
                "note": f"Review {public_id} was superseded by {replacement_id}.",
            }
    if stage == STAGE_DECISION_PENDING:
        return {
            "cmd": _review_command(public_id, "--decision", DECISION_CLEAN),
            "alt": _review_command(public_id, "--decision", DECISION_FINDINGS),
        }
    if stage == STAGE_FIX_PENDING:
        return {
            "cmd": _review_command(public_id),
            "note": "Fix valid findings, then rerun this command.",
        }
    if stage in {STAGE_CREATED, STAGE_GATE_RERUN_NEEDED}:
        action = {"cmd": _review_command(public_id)}
        note = _continuation_note(state)
        if note:
            action["note"] = note
        return action
    if stage in {STAGE_REVIEW_GREEN, STAGE_LOCAL_GREEN_HANDOFF}:
        return _github_handoff_action(state, state_dir=state_dir)
    return {"cmd": _review_command(public_id)}


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
        if args.reason and not args.restart_mode:
            raise ValueError("--reason requires --restart-mode")
        if args.restart_mode and (args.decision or args.github_review or args.github_force or has_validation_status):
            raise ValueError("--restart-mode cannot be combined with decisions, GitHub review, or validation status flags")
        if args.decision and not args.id:
            raise ValueError("--decision requires --id")
        if args.github_force and not args.github_review:
            raise ValueError("--github-force requires --github-review")
        if args.github_review and not args.id:
            raise ValueError("--github-review requires --id")
        if args.github_review and (args.decision or has_validation_status):
            raise ValueError("--github-review cannot be combined with decisions or validation status flags")
        if has_validation_status and not args.id:
            raise ValueError("validation status flags require --id")
        if args.id:
            state_dir, state = _load_cycle_and_state_dir(state_dir, str(args.id))
            _reject_id_creation_args(args, state)
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
