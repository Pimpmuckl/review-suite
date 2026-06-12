#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from review_suite_runtime_bootstrap import bootstrap_from_installed_cache

bootstrap_from_installed_cache(__file__)

from review_suite_core import AxiArgumentParser, emit_error, emit_toon, format_command, inspect_workflow_status, resolve_repo_root
from review_suite_core.orchestrator_profiles import RESTART_MODE_ORDER
from review_gate import gate_signoff_action_payload, gate_signoff_decisions_by_round, load_gate_record, pending_gate_signoff_records
from review_suite_local import (
    default_state_dir,
    load_round,
    normalize_record_review_cwd_value,
    normalize_review_cwd_value,
    public_task_name,
    read_jsonl,
    round_needs_caller_grade,
    terminal_review_command,
    unique_round_state_dirs,
)

GATE_TASK_TO_REVIEW_LANE = {
    "phase_gate": "review_t2",
    "pr_gate": "review_t4",
}
GATE_FINDINGS_RERUN_REASON = {
    "review_t2": "t2_findings_followup_needs_signoff",
    "review_t4": "t4_findings_followup_needs_signoff",
}
ORCHESTRATOR_HIDDEN_STAGES = {"aborted", "dismissed"}
ORCHESTRATOR_HEAD_CURRENT_STAGES = {"review-green", "local-green-handoff"}
VALIDATION_READY_STATUSES = {"passed", "waived", "classified"}
DECISION_COMMANDS = {"clean", "findings"}


def build_parser() -> argparse.ArgumentParser:
    parser = AxiArgumentParser(description="Inspect deterministic review workflow state for the current branch.")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=AxiArgumentParser)

    status = sub.add_parser("status")
    status.add_argument("--cd")
    status.add_argument("--base", default="main")
    status.add_argument("--wsl", action="store_true", help=argparse.SUPPRESS)
    status.add_argument("--state-dir", default=str(default_state_dir()), help=argparse.SUPPRESS)
    status.add_argument("--verbose", action="store_true", help="Print the full routing snapshot.")
    return parser


def _help_command() -> str:
    return format_command([sys.executable, str(Path(__file__).resolve()), "--help"])


def _wsl_unc_cd(cd: str | None) -> bool:
    normalized = str(cd or "").strip().replace("\\", "/").lower()
    return normalized.startswith("//wsl.localhost/") or normalized.startswith("//wsl$/")


def _reject_review_state_unc_wsl(cd: str | None) -> None:
    if sys.platform != "win32" or not _wsl_unc_cd(cd):
        return
    raise ValueError(
        "review_state does not launch Codex, so --wsl is not useful here. "
        "Run review_state from native WSL with the Linux repo path instead of a Windows UNC path; Windows git over //wsl.localhost can hang."
    )


def _arena_show_round_command(*, round_id: str) -> str:
    return format_command(
        [
            sys.executable,
            Path(__file__).resolve().with_name("review_suite_arena.py").as_posix(),
            "show-round",
            "--round-id",
            round_id,
        ]
    )


def _review_mode_for_lane(lane: str) -> str:
    return "deep" if lane in {"review_t3", "review_t4"} else "normal"


def _start_review_command(*, review_cwd: Path, base: str, mode: str) -> str:
    return format_command(
        [
            sys.executable,
            Path(__file__).resolve().with_name("review.py").as_posix(),
            "--mode",
            mode,
            "--cd",
            str(review_cwd),
            "--base",
            base,
        ]
    )


def _gate_signoff_override(
    *,
    state_dir: Path,
    review_cwd: Path,
    base: str,
    current_payload: dict[str, object],
) -> dict[str, object] | None:
    pending = pending_gate_signoff_records(
        state_dir=state_dir,
        review_cwd=review_cwd,
        base=base,
    )
    if not pending:
        return None
    latest = pending[-1]
    round_id = str(latest.get("round_id") or "").strip()
    task_class = str(latest.get("task_class") or "").strip()
    scope = dict(latest.get("review_scope") or {})
    reviewed_head = str(scope.get("reviewed_head") or scope.get("commit_end") or scope.get("commit") or "").strip()
    current_head = str(current_payload.get("head") or "").strip()
    head_matches_current = bool(reviewed_head and current_head and reviewed_head == current_head)
    note = "View the round, then close the gate as clean or findings."
    if reviewed_head and current_head and reviewed_head != current_head:
        note = "Reviewed head moved since this gate ran. View the round, close the gate for that head, then rerun review-state."
    return {
        "recommendation": "signoff-decision",
        "reason": "pending_gate_signoff_decision",
        "note": note,
        "pending_round_id": round_id,
        "pending_round_status": "signoff_pending",
        "pending_round_task": public_task_name(task_class),
        "pending_round_task_id_hint": str(latest.get("task_id") or round_id),
        "pending_round_reviewed_head": reviewed_head,
        "pending_round_current_head": current_head,
        "pending_round_head_matches_current": head_matches_current,
    }


def _gate_findings_rerun_override(
    *,
    state_dir: Path,
    review_cwd: Path,
    base: str,
    current_payload: dict[str, object],
) -> dict[str, object] | None:
    if str(current_payload.get("recommendation") or "") != "none":
        return None
    if str(current_payload.get("last_reviewed_lane") or "") != "review-followup":
        return None
    head = str(current_payload.get("head") or "").strip()
    if not head:
        return None
    branch = str(current_payload.get("branch") or "").strip()
    normalized_cwd = str(normalize_review_cwd_value(review_cwd) or "")
    decisions = gate_signoff_decisions_by_round(state_dir)
    relevant: list[dict[str, object]] = []
    clean_current_head = False
    for record in read_jsonl(state_dir / "gate_runs.jsonl"):
        task_class = str(record.get("task_class") or "")
        lane = GATE_TASK_TO_REVIEW_LANE.get(task_class)
        if lane is None:
            continue
        record_cwd = str(normalize_record_review_cwd_value(record) or "")
        if record_cwd != normalized_cwd:
            continue
        scope = dict(record.get("review_scope") or {})
        if str(base or "").strip() and str(scope.get("base") or "").strip() != str(base or "").strip():
            continue
        task_id = str(record.get("task_id") or "").strip()
        if branch and branch != "HEAD" and task_id and task_id != branch:
            continue
        round_id = str(record.get("round_id") or "").strip()
        decision = decisions.get(round_id) or {}
        verdict = str(decision.get("verdict") or "").strip()
        record_head = str(scope.get("reviewed_head") or scope.get("commit_end") or scope.get("commit") or "").strip()
        if verdict == "clean" and record_head == head:
            clean_current_head = True
        relevant.append({**dict(record), "_lane": lane, "_verdict": verdict, "_record_head": record_head})
    if clean_current_head or not relevant:
        return None
    latest = sorted(
        relevant,
        key=lambda item: str(item.get("review_completed_at") or item.get("recorded_at") or item.get("round_id") or ""),
    )[-1]
    if str(latest.get("_verdict") or "") != "findings":
        return None
    lane = str(latest.get("_lane") or "")
    reason = GATE_FINDINGS_RERUN_REASON.get(lane)
    if not reason:
        return None
    round_id = str(latest.get("round_id") or "")
    reviewed_head = str(latest.get("_record_head") or "")
    lane_short = lane.replace("review_", "")
    label = lane_short.upper()
    return {
        "recommendation": "full-review",
        "reason": reason,
        "recommended_lane": lane,
        "last_gate_findings_round_id": round_id,
        "last_gate_findings_reviewed_head": reviewed_head,
        f"last_{lane_short}_round_id": round_id,
        f"last_{lane_short}_reviewed_head": reviewed_head,
        "note": (
            f"The latest {label} gate for this branch was closed as findings and the current head has a clean follow-up anchor. "
            f"Rerun {lane} so all signoff reviewers are effectively green on the current head."
        ),
    }


def _with_action_context(action: dict[str, object], *, review_cwd: Path, round_id: str | None = None) -> dict[str, object]:
    action["cwd"] = str(review_cwd)
    if round_id:
        action["round_id"] = round_id
    return action


def _public_status_action(action: dict[str, object]) -> dict[str, object]:
    hidden = {"lane", "round_id", "source_gate_round_id"}
    return {key: value for key, value in action.items() if key not in hidden and value not in (None, "", [], {})}


def _public_status_payload(payload: dict[str, object]) -> dict[str, object]:
    public: dict[str, object] = {}
    for key in ("review", "progress", "recommendation", "reason"):
        value = payload.get(key)
        if value not in (None, "", [], {}):
            public[key] = value

    if payload.get("pending_round_head_matches_current") is False:
        reviewed_head = str(payload.get("pending_round_reviewed_head") or "").strip()
        current_head = str(payload.get("pending_round_current_head") or "").strip()
        if reviewed_head:
            public["reviewed_head"] = reviewed_head
        if current_head:
            public["current_head"] = current_head
        note = str(payload.get("note") or "").strip()
        if note:
            public["note"] = note

    action = payload.get("Action")
    if isinstance(action, dict) and action:
        public["Action"] = _public_status_action(action)
    elif str(payload.get("note") or "").strip():
        public["note"] = str(payload.get("note") or "").strip()

    return public or payload


def _state_dir_args(state_dir: Path) -> list[str]:
    resolved = state_dir.resolve(strict=False)
    if resolved == Path(default_state_dir()).resolve(strict=False):
        return []
    return ["--state-dir", str(resolved)]


def _review_command(public_id: str, *, state_dir: Path, extra: tuple[str, ...] = ()) -> str:
    return format_command(
        [
            sys.executable,
            Path(__file__).resolve().with_name("review.py").as_posix(),
            "--id",
            public_id,
            *extra,
            *_state_dir_args(state_dir),
        ]
    )


def _restart_deep_action(state: dict[str, object], public_id: str, *, state_dir: Path) -> dict[str, object] | None:
    stage = str(state.get("stage") or "").strip()
    if stage in ORCHESTRATOR_HIDDEN_STAGES or isinstance(state.get("superseded_by"), dict):
        return None
    mode = str(dict(state.get("mode") or {}).get("effective") or dict(state.get("mode") or {}).get("requested") or "").strip()
    if mode not in RESTART_MODE_ORDER or RESTART_MODE_ORDER[mode] >= RESTART_MODE_ORDER["deep"]:
        return None
    return {
        "cmd": _review_command(public_id, state_dir=state_dir, extra=("--restart-mode", "deep", "--reason", "REASON")),
        "mode": "deep",
        "note": "Use only for explicit escalation; replace REASON.",
    }


def _orchestrator_cycles(state_dir: Path) -> list[dict[str, object]]:
    cycles: list[dict[str, object]] = []
    for path in sorted((state_dir / "orchestrator" / "cycles").glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            try:
                payload["_state_file_mtime"] = path.stat().st_mtime
            except OSError:
                payload["_state_file_mtime"] = 0.0
            cycles.append(payload)
    return cycles


def _review_step_position(state: dict[str, object], step_index: int) -> tuple[int | None, int | None]:
    steps = [item for item in list(dict(state.get("review_plan") or {}).get("steps") or []) if isinstance(item, dict)]
    review_indices = [
        index
        for index, item in enumerate(steps)
        if str(item.get("kind") or "review").strip() == "review"
    ]
    if step_index not in review_indices:
        return None, None
    return review_indices.index(step_index) + 1, len(review_indices)


def _orchestrator_progress_label(state: dict[str, object]) -> str | None:
    candidates: list[dict[str, object]] = []
    pending = dict(state.get("pending_action") or {})
    if pending:
        candidates.append(pending)
    current_step = dict(dict(state.get("review_progress") or {}).get("current_step") or {})
    if current_step:
        candidates.append(current_step)
    for item in candidates:
        try:
            step_index = int(item.get("step_index") if item.get("step_index") is not None else item.get("index"))
        except (TypeError, ValueError):
            continue
        step_name = str(item.get("step") or item.get("name") or "").strip()
        lane = str(item.get("lane") or "review_t1").strip()
        if lane != "review_t1" or not step_name:
            continue
        position, total = _review_step_position(state, step_index)
        if position and total:
            return f"review {position}/{total} {step_name}"
    return None


def _round_by_id(state: dict[str, object], round_id: str) -> dict[str, object]:
    for item in list(state.get("rounds") or []):
        if isinstance(item, dict) and str(item.get("round_id") or "") == round_id:
            return dict(item)
    return {}


def _orchestrator_review_state_dir(state_dir: Path) -> Path:
    return state_dir / "orchestrator" / "review-rounds"


def _round_state_dir_candidates(state_dir: Path, round_record: dict[str, object]) -> list[Path]:
    candidates: list[Path] = []
    round_state_dir = str(round_record.get("round_state_dir") or "").strip()
    if round_state_dir:
        candidates.append(Path(round_state_dir))
    candidates.extend([_orchestrator_review_state_dir(state_dir), state_dir])
    return unique_round_state_dirs(candidates)


def _fallback_round_payload(round_record: dict[str, object]) -> dict[str, object]:
    payload = dict(round_record)
    runs: list[dict[str, object]] = []
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


def _load_output_round_payload(state_dir: Path, round_record: dict[str, object]) -> dict[str, object]:
    round_id = str(round_record.get("round_id") or "").strip()
    if not round_id:
        return _fallback_round_payload(round_record)
    for candidate in _round_state_dir_candidates(state_dir, round_record):
        try:
            payload = load_round(candidate, round_id)
            payload.setdefault("_round_state_dir", str(candidate))
            return payload
        except ValueError:
            continue
    gate_record = load_gate_record(state_dir, round_id)
    if gate_record is not None:
        return gate_record
    return _fallback_round_payload(round_record)


def _pending_grade_payload(state: dict[str, object], *, state_dir: Path) -> dict[str, object] | None:
    pending = dict(state.get("pending_action") or {})
    round_id = str(pending.get("round_id") or "").strip()
    if str(pending.get("kind") or "") != "decision" or not round_id:
        return None
    round_record = _round_by_id(state, round_id)
    if not bool(round_record.get("grading_required")):
        return None
    payload = _load_output_round_payload(state_dir, round_record)
    if not bool(round_record.get("arena_round") or payload.get("arena_round")):
        return None
    if not round_needs_caller_grade(payload):
        return None
    return payload


def _arena_grade_command(state: dict[str, object], *, state_dir: Path) -> str | None:
    pending_payload = _pending_grade_payload(state, state_dir=state_dir)
    if pending_payload is None:
        return None
    round_id = str(pending_payload.get("round_id") or "").strip()
    task_id = str(pending_payload.get("task_id_hint") or "").strip()
    if not task_id:
        task_id = str(dict(state.get("identity") or {}).get("branch") or state.get("public_id") or "").strip()
    grade_state_dir = Path(str(pending_payload.get("_round_state_dir") or state_dir))
    return format_command(
        [
            sys.executable,
            Path(__file__).resolve().with_name("review_suite_arena.py").as_posix(),
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
            str(grade_state_dir),
        ]
    )


def _round_blocked(round_record: dict[str, object]) -> bool:
    if bool(round_record.get("review_blocked")):
        return True
    for run in list(round_record.get("runs") or []):
        if not isinstance(run, dict):
            continue
        if bool(run.get("blocked")) or bool(run.get("grade_blocked")):
            return True
    return False


def _round_terminal_command(round_record: dict[str, object]) -> str | None:
    if bool(round_record.get("grading_required")) and bool(round_record.get("needs_grade")) and not bool(round_record.get("graded")):
        return None
    commands: list[str] = []
    for run in list(round_record.get("runs") or []):
        if not isinstance(run, dict):
            continue
        status = str(run.get("review_status") or run.get("status") or "").strip()
        if status and status != "completed":
            return None
        command = str(run.get("terminal_command") or "").strip().lower()
        if not command:
            command = terminal_review_command(str(run.get("reviewer_output") or ""))
        if command not in DECISION_COMMANDS:
            return None
        commands.append(command)
    if not commands:
        return None
    return "findings" if "findings" in commands else "clean"


def _auto_decision_command(state: dict[str, object], *, state_dir: Path) -> str | None:
    pending = dict(state.get("pending_action") or {})
    if str(pending.get("kind") or "") != "decision":
        return None
    round_id = str(pending.get("round_id") or "").strip()
    if not round_id:
        return None
    if _pending_grade_payload(state, state_dir=state_dir) is not None:
        return None
    round_record = _round_by_id(state, round_id)
    if not round_record:
        return None
    round_payload = _load_output_round_payload(state_dir, round_record)
    if _round_blocked(round_record) or _round_blocked(round_payload):
        return None
    return _round_terminal_command(round_payload)


def _orchestrator_action(state: dict[str, object], public_id: str, *, state_dir: Path) -> dict[str, object]:
    stage = str(state.get("stage") or "").strip()
    superseded_by = state.get("superseded_by")
    if stage == "aborted" and isinstance(superseded_by, dict):
        replacement = str(superseded_by.get("review") or "").strip()
        if replacement:
            return {
                "cmd": _review_command(replacement, state_dir=state_dir),
                "note": f"Review {public_id} was superseded by {replacement}.",
            }
    if stage == "decision-pending":
        grade = _arena_grade_command(state, state_dir=state_dir)
        if grade:
            action = {
                "cmd": grade,
                "note": "Grade the arena round, then rerun this review id to continue.",
                "next": _review_command(public_id, state_dir=state_dir),
            }
        else:
            auto_decision = _auto_decision_command(state, state_dir=state_dir)
            if auto_decision:
                action: dict[str, object] = {
                    "cmd": _review_command(public_id, state_dir=state_dir),
                    "note": f"Structured {auto_decision} verdict is ready; rerun this review id to record it and continue.",
                    "override": {
                        "clean": _review_command(public_id, state_dir=state_dir, extra=("--decision", "clean")),
                        "findings": _review_command(public_id, state_dir=state_dir, extra=("--decision", "findings")),
                    },
                }
            else:
                action = {
                    "cmd": _review_command(public_id, state_dir=state_dir, extra=("--decision", "clean")),
                    "alt": _review_command(public_id, state_dir=state_dir, extra=("--decision", "findings")),
                }
    elif stage == "fix-pending":
        action = {
            "cmd": _review_command(public_id, state_dir=state_dir),
            "note": "Commit/amend valid fixes, then rerun this command.",
        }
    elif stage in {"review-green", "local-green-handoff"}:
        action = {
            "cmd": _review_command(public_id, state_dir=state_dir, extra=("--github-review",)),
            "after": "PR create/update",
        }
        blockers = []
        validation = dict(state.get("validation") or {})
        for key in ("full_suite", "ci"):
            value = str(validation.get(key) or "unknown").strip() or "unknown"
            if value not in VALIDATION_READY_STATUSES:
                blockers.append(f"{key}:{value}")
        if blockers:
            action["blocked_by"] = blockers
    else:
        action = {"cmd": _review_command(public_id, state_dir=state_dir)}
    restart = _restart_deep_action(state, public_id, state_dir=state_dir)
    if restart:
        action["restart"] = restart
    return action


def _orchestrator_review_head(state: dict[str, object]) -> str:
    review_heads = dict(state.get("review_heads") or {})
    for key in ("last_gate_clean_head", "last_followup_head", "last_reviewed_head", "last_fix_head", "head"):
        value = str(review_heads.get(key) or "").strip()
        if value:
            return value
    return str(dict(state.get("identity") or {}).get("head") or "").strip()


def _orchestrator_cycle_is_current(state: dict[str, object], current_payload: dict[str, object]) -> bool:
    if str(state.get("stage") or "") not in ORCHESTRATOR_HEAD_CURRENT_STAGES:
        return True
    current_head = str(current_payload.get("head") or "").strip()
    reviewed_head = _orchestrator_review_head(state)
    return not current_head or not reviewed_head or current_head == reviewed_head


def _orchestrator_status_override(
    *,
    state_dir: Path,
    review_cwd: Path,
    base: str,
    current_payload: dict[str, object],
) -> dict[str, object] | None:
    normalized_cwd = str(normalize_review_cwd_value(review_cwd) or "")
    branch = str(current_payload.get("branch") or "").strip()
    candidates: list[dict[str, object]] = []
    for state in _orchestrator_cycles(state_dir):
        identity = dict(state.get("identity") or {})
        if str(identity.get("cwd") or "") != normalized_cwd:
            continue
        if str(identity.get("base") or "") != str(base or "").strip():
            continue
        cycle_branch = str(identity.get("branch") or "").strip()
        if branch and cycle_branch and cycle_branch != branch:
            continue
        if str(state.get("stage") or "") in ORCHESTRATOR_HIDDEN_STAGES:
            continue
        if not _orchestrator_cycle_is_current(state, current_payload):
            continue
        if not str(state.get("public_id") or "").strip():
            continue
        candidates.append(state)
    if not candidates:
        return None
    state = sorted(
        candidates,
        key=lambda item: (
            float(item.get("_state_file_mtime") or 0.0),
            str(item.get("updated_at") or item.get("created_at") or item.get("public_id") or ""),
        ),
    )[-1]
    public_id = str(state.get("public_id") or "").strip()
    payload: dict[str, object] = {
        "review": public_id,
        "Action": _orchestrator_action(state, public_id, state_dir=state_dir),
    }
    progress = _orchestrator_progress_label(state)
    if progress:
        payload["progress"] = progress
    return payload


def _status_action(payload: dict[str, object], *, review_cwd: Path, base: str, state_dir: Path) -> dict[str, object] | None:
    recommendation = str(payload.get("recommendation") or "")
    if recommendation == "signoff-decision":
        round_id = str(payload.get("pending_round_id") or "").strip()
        if not round_id:
            return None
        return _with_action_context(gate_signoff_action_payload(round_id=round_id, state_dir=state_dir), review_cwd=review_cwd, round_id=round_id)
    if recommendation == "fix-gate-findings":
        round_id = str(payload.get("last_gate_findings_round_id") or "").strip()
        if not round_id:
            return None
        return _with_action_context(
            {
                "lane": "gate-findings",
                "show_cmd": _arena_show_round_command(round_id=round_id),
                "note": "View the round, fix valid bugs, then rerun review-state.",
            },
            review_cwd=review_cwd,
            round_id=round_id,
        )
    if recommendation == "review-followup":
        since_head = str(payload.get("last_reviewed_head") or "").strip()
        if not since_head:
            return None
        if bool(payload.get("worktree_dirty")):
            return _with_action_context(
                {
                    "lane": "commit-or-stash",
                    "note": (
                        "Commit intended follow-up changes or stash unrelated dirty files, "
                        "then rerun review-state to get the review-followup command."
                    ),
                },
                review_cwd=review_cwd,
            )
        command = [
            sys.executable,
            Path(__file__).resolve().with_name("review_followup.py").as_posix(),
            "--base",
            base,
            "--since",
            since_head,
        ]
        command.extend(
            [
                "--note-file",
                ".review-suite/fix-note.md",
            ]
        )
        action = {
            "lane": "review-followup",
            "cmd": format_command(command),
        }
        source_gate_round_id = str(payload.get("last_gate_findings_round_id") or "").strip()
        if source_gate_round_id:
            action["source_gate_round_id"] = source_gate_round_id
        return _with_action_context(action, review_cwd=review_cwd)
    recommended_lane = str(payload.get("recommended_lane") or "").strip()
    if recommendation in {"coherence-review", "full-review"} and recommended_lane in {
        "review_t1",
        "review_t2",
        "review_t3",
        "review_t4",
    }:
        return _with_action_context(
            {
                "lane": recommended_lane,
                "cmd": _start_review_command(review_cwd=review_cwd, base=base, mode=_review_mode_for_lane(recommended_lane)),
            },
            review_cwd=review_cwd,
        )
    if recommendation == "coherence-review":
        return _with_action_context(
            {
                "lane": "coherence-review",
                "cmd": _start_review_command(review_cwd=review_cwd, base=base, mode="normal"),
            },
            review_cwd=review_cwd,
        )
    if recommendation == "full-review":
        return _with_action_context(
            {
                "lane": "full-review",
                "cmd": _start_review_command(review_cwd=review_cwd, base=base, mode="normal"),
            },
            review_cwd=review_cwd,
        )
    return None


def cmd_status(args: argparse.Namespace) -> int:
    _reject_review_state_unc_wsl(getattr(args, "cd", None))
    review_cwd = resolve_repo_root(args.cd)
    state_dir = Path(args.state_dir)
    payload = inspect_workflow_status(
        state_dir=state_dir,
        review_cwd=review_cwd,
        base=str(args.base),
    )
    orchestrator_override = _orchestrator_status_override(
        state_dir=state_dir,
        review_cwd=review_cwd,
        base=str(args.base),
        current_payload=payload,
    )
    if orchestrator_override is not None:
        payload = orchestrator_override
    else:
        signoff_override = _gate_signoff_override(
            state_dir=state_dir,
            review_cwd=review_cwd,
            base=str(args.base),
            current_payload=payload,
        )
        if signoff_override is not None:
            payload.update(signoff_override)
        else:
            gate_findings_override = _gate_findings_rerun_override(
                state_dir=state_dir,
                review_cwd=review_cwd,
                base=str(args.base),
                current_payload=payload,
            )
            if gate_findings_override is not None:
                payload.update(gate_findings_override)
    action = _status_action(payload, review_cwd=review_cwd, base=str(args.base), state_dir=state_dir)
    if action is not None:
        payload["Action"] = action
    emit_toon(payload if bool(args.verbose) else _public_status_payload(payload))
    return 0


def main() -> int:
    parser = build_parser()
    try:
        args = parser.parse_args()
        if args.command == "status":
            return cmd_status(args)
    except ValueError as exc:
        return emit_error(
            str(exc),
            status="usage_error",
            help_items=[_help_command()],
        )
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
