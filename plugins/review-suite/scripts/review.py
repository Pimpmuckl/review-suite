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
from review_suite_core.orchestrator_state import (
    DECISION_CLEAN,
    DECISION_FINDINGS,
    STAGE_CREATED,
    STAGE_DECISION_PENDING,
    STAGE_FIX_PENDING,
    STAGE_FOLLOWUP_PENDING,
    STAGE_REVIEW_GREEN,
    create_cycle,
    mark_decision_pending,
    mark_fix_detected,
    record_clean_decision,
    record_findings_decision,
    record_followup_clean,
    record_followup_findings,
)
from review_suite_core.orchestrator_store import load_cycle_by_key, load_cycle_by_public_id, save_cycle


INITIAL_LANE = "review"
FOLLOWUP_LANE = "review-followup"


def build_parser() -> argparse.ArgumentParser:
    parser = AxiArgumentParser(description="Run the review-suite orchestrator shell.")
    parser.add_argument("--id")
    parser.add_argument("--mode", choices=SUPPORTED_MODES)
    parser.add_argument("--selection", choices=SUPPORTED_SELECTIONS, default="auto")
    parser.add_argument("--cd")
    parser.add_argument("--base", default="main")
    parser.add_argument("--decision", choices=(DECISION_CLEAN, DECISION_FINDINGS))
    parser.add_argument("--state-dir", default=str(default_state_dir()), help=argparse.SUPPRESS)
    return parser


def _help_command() -> str:
    return format_command([sys.executable, str(Path(__file__).resolve()), "--help"])


def _review_command(public_id: str, *extra: str) -> str:
    return format_command([sys.executable, str(Path(__file__).resolve()), "--id", public_id, *extra])


def _next_round_id(state: dict[str, Any]) -> str:
    return f"round-{len(list(state.get('rounds') or [])) + 1}"


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


def _open_decision(state: dict[str, Any], *, lane: str, reviewed_head: str | None = None) -> dict[str, Any]:
    if state.get("stage") == STAGE_DECISION_PENDING:
        pending = dict(state.get("pending_action") or {})
        if pending.get("kind") == "decision" and pending.get("round_id") and pending.get("lane"):
            return state
    round_id = _next_round_id(state)
    pending_action: dict[str, Any] = {
        "kind": "decision",
        "round_id": round_id,
        "lane": lane,
    }
    active = state.get("active_findings")
    if lane == FOLLOWUP_LANE and isinstance(active, dict):
        pending_action["source_round_id"] = active.get("round_id")
    return mark_decision_pending(
        state,
        round_id=round_id,
        lane=lane,
        reviewed_head=reviewed_head,
        pending_action=pending_action,
    )


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
        lane = FOLLOWUP_LANE if stage == STAGE_FOLLOWUP_PENDING else INITIAL_LANE
        return _open_decision(state, lane=lane, reviewed_head=_identity_head(state))
    if stage != STAGE_FIX_PENDING:
        return state

    active = dict(state.get("active_findings") or {})
    reviewed_head = str(active.get("reviewed_head") or "").strip()
    head = _identity_head(state)
    if head and reviewed_head and head != reviewed_head:
        fixed = mark_fix_detected(state, head=head)
        return _open_decision(fixed, lane=FOLLOWUP_LANE, reviewed_head=head)
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
    )
    existing = load_cycle_by_key(state_dir, str(state["cycle_key"]))
    if existing is not None:
        return _resume_progress(existing)
    state["grading"] = {"required": bool(resolution.requires_grading)}
    return _open_decision(state, lane=INITIAL_LANE, reviewed_head=head)


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
    if stage == STAGE_REVIEW_GREEN:
        return {"status": "none"}
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
        if args.decision and not args.id:
            raise ValueError("--decision requires --id")
        if args.id:
            state = load_cycle_by_public_id(state_dir, str(args.id))
            state = _apply_decision(state, str(args.decision)) if args.decision else _resume_progress(state)
        else:
            state = _create_or_resume_cycle(args=args, state_dir=state_dir)
        saved = save_cycle(state_dir, state)
        _render(saved)
        return 0
    except ValueError as exc:
        return emit_error(str(exc), status="usage_error", help_items=[_help_command()])


if __name__ == "__main__":
    raise SystemExit(main())
