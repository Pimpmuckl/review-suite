#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from review_suite_core import (
    AxiArgumentParser,
    current_branch,
    current_head,
    cwd_path_from_normalized,
    emit_error,
    emit_toon,
    format_command,
    merge_base,
    resolve_repo_root,
)
from review_suite_core.config import default_state_dir, load_config
from review_suite_core.orchestrator_profiles import SUPPORTED_MODES, SUPPORTED_SELECTIONS, resolve_orchestrator_profile
from review_suite_core.orchestrator_runner import run_one_expensive_step
from review_suite_core.orchestrator_state import (
    DECISION_CLEAN,
    DECISION_FINDINGS,
    STAGE_CREATED,
    STAGE_DECISION_PENDING,
    STAGE_FIX_PENDING,
    STAGE_FOLLOWUP_PENDING,
    STAGE_LOCAL_GREEN_HANDOFF,
    STAGE_REVIEW_GREEN,
    create_cycle,
    mark_fix_detected,
    record_clean_decision,
    record_findings_decision,
    record_followup_clean,
    record_followup_findings,
    record_validation_statuses,
)
from review_suite_core.orchestrator_store import load_cycle_by_key, load_cycle_by_public_id, save_cycle


FOLLOWUP_LANE = "review-followup"
CLI_VALIDATION_STATUSES = ("passed", "failed", "pending", "waived", "classified")
VALIDATION_READY_STATUSES = {"passed", "waived", "classified"}


def build_parser() -> argparse.ArgumentParser:
    parser = AxiArgumentParser(description="Run the review-suite orchestrator shell.")
    parser.add_argument("--id")
    parser.add_argument("--mode", choices=SUPPORTED_MODES)
    parser.add_argument("--selection", choices=SUPPORTED_SELECTIONS, default="auto")
    parser.add_argument("--cd")
    parser.add_argument("--base", default="main")
    parser.add_argument("--decision", choices=(DECISION_CLEAN, DECISION_FINDINGS))
    parser.add_argument("--focused-validation", choices=CLI_VALIDATION_STATUSES)
    parser.add_argument("--full-suite", choices=CLI_VALIDATION_STATUSES)
    parser.add_argument("--ci", choices=CLI_VALIDATION_STATUSES)
    parser.add_argument("--state-dir", default=str(default_state_dir()), help=argparse.SUPPRESS)
    return parser


def _help_command() -> str:
    return format_command([sys.executable, str(Path(__file__).resolve()), "--help"])


def _review_command(public_id: str, *extra: str) -> str:
    return format_command([sys.executable, str(Path(__file__).resolve()), "--id", public_id, *extra])


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


def _create_or_resume_cycle(*, args: argparse.Namespace, state_dir: Path) -> dict[str, Any]:
    if not args.mode:
        raise ValueError("--mode is required when creating a review cycle")
    review_root = resolve_repo_root(args.cd)
    head = current_head(review_root)
    branch = current_branch(review_root)
    merge_base_head = merge_base(review_root, str(args.base), "HEAD")
    resolution = resolve_orchestrator_profile(load_config(state_dir), mode=str(args.mode), selection=str(args.selection))
    state = create_cycle(
        cwd=review_root,
        base=str(args.base),
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
    state["grading"] = {"required": bool(resolution.requires_grading)}
    state["review_plan"] = {
        "steps": [
            {
                "name": step.name,
                "count": step.count,
                "model": step.model,
                "reasoning_effort": step.reasoning_effort,
                "service_tier": step.service_tier,
            }
            for step in resolution.steps
        ],
    }
    return state


def _advance_without_decision(state: dict[str, Any], *, state_dir: Path) -> dict[str, Any]:
    ready_state = _resume_progress(state)
    result = run_one_expensive_step(ready_state, state_dir=state_dir)
    if result.ran_step:
        return result.state
    return _resume_progress(ready_state)


def _apply_decision(state: dict[str, Any], decision: str) -> dict[str, Any]:
    ready_state = _resume_progress(state)
    if ready_state.get("stage") != STAGE_DECISION_PENDING:
        raise ValueError("no decision is pending for this review cycle")
    round_id, lane = _pending_decision(ready_state)
    reviewed_head = str(_round_by_id(ready_state, round_id).get("reviewed_head") or "").strip() or None
    if decision == DECISION_CLEAN:
        if lane == FOLLOWUP_LANE:
            return record_followup_clean(ready_state, round_id=round_id, reviewed_head=reviewed_head)
        return record_clean_decision(ready_state, round_id=round_id, lane=lane, reviewed_head=reviewed_head)
    if decision == DECISION_FINDINGS:
        if lane == FOLLOWUP_LANE:
            return _with_fix_action(record_followup_findings(ready_state, round_id=round_id, reviewed_head=reviewed_head))
        return _with_fix_action(record_findings_decision(ready_state, round_id=round_id, lane=lane, reviewed_head=reviewed_head))
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


def _github_handoff_action(state: dict[str, Any]) -> dict[str, Any]:
    action: dict[str, Any] = {
        "kind": "github-handoff",
        "lane": "review-github",
        "after": "PR create/update",
        "github_review": "not-run",
    }
    blockers = _validation_blockers(state)
    if blockers:
        action["validation_ready"] = False
        action["blocked_by"] = blockers
    else:
        action["validation_ready"] = True
    return action


def _action_payload(state: dict[str, Any]) -> dict[str, Any] | None:
    public_id = str(state.get("public_id") or "").strip()
    stage = state.get("stage")
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
    if stage in {STAGE_REVIEW_GREEN, STAGE_LOCAL_GREEN_HANDOFF}:
        return _github_handoff_action(state)
    return {"cmd": _review_command(public_id)}


def _render(state: dict[str, Any]) -> None:
    mode = dict(state.get("mode") or {})
    selection = dict(state.get("selection") or {})
    payload: dict[str, Any] = {
        "review": state.get("public_id"),
        "stage": state.get("stage"),
        "mode": mode.get("effective") or mode.get("requested"),
        "selection": selection.get("effective") or selection.get("requested"),
    }
    grading = dict(state.get("grading") or {})
    if grading.get("required"):
        payload["grading"] = "required"
    action = _action_payload(state)
    if action:
        payload["Action"] = action
    emit_toon(payload)


def main() -> int:
    parser = build_parser()
    try:
        args = parser.parse_args()
        state_dir = Path(args.state_dir)
        has_validation_status = _has_validation_status(args)
        if args.decision and not args.id:
            raise ValueError("--decision requires --id")
        if has_validation_status and not args.id:
            raise ValueError("validation status flags require --id")
        if args.id:
            state = load_cycle_by_public_id(state_dir, str(args.id))
            if args.decision:
                state = _apply_decision(state, str(args.decision))
            if has_validation_status:
                state = _record_validation_status(state, args)
            elif not args.decision:
                state = _advance_without_decision(state, state_dir=state_dir)
        else:
            state = _create_or_resume_cycle(args=args, state_dir=state_dir)
            state = _advance_without_decision(state, state_dir=state_dir)
        saved = save_cycle(state_dir, state)
        _render(saved)
        return 0
    except ValueError as exc:
        return emit_error(str(exc), status="usage_error", help_items=[_help_command()])


if __name__ == "__main__":
    raise SystemExit(main())
