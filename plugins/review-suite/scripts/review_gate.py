#!/usr/bin/env python3
from __future__ import annotations

import json
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import blake2s
from pathlib import Path
from typing import Any

from review_suite_core import format_command, gate_config, utc_now, utc_now_iso, wrapper_launch_cwd, write_text
from review_suite_local import (
    CAPACITY_RETRY_DELAY_SECONDS,
    CAPACITY_RETRY_MAX_ATTEMPTS,
    MULTI_REVIEW_DISPATCH_STAGGER_SECONDS,
    OPERATIONAL_STATE_FILENAME,
    STALE_REVIEW_STATE_TTL_SECONDS,
    _active_cooldowns,
    _apply_capacity_cooldowns,
    _parse_timestamp,
    _progress_status_line,
    _print_stall_warnings,
    _print_transport_events,
    _process_is_running,
    _transport_hung_after_output,
    _terminate_process_tree,
    _transport_stalled,
    append_jsonl,
    build_review_command,
    collect_completed_review_capture,
    ensure_clean_git_worktree,
    format_cooldown_until_for_display,
    format_compact_tokens,
    format_cost_cents,
    format_decimal,
    load_operational_state,
    load_roster,
    includes_deep_review_effort,
    make_round_id,
    normalize_record_review_cwd_value,
    normalize_review_cwd_value,
    pending_launch_ready,
    public_reviewer_label,
    print_deep_review_wait_note,
    read_jsonl,
    reviewer_completion_status,
    reviewer_output_heading,
    state_lock,
    total_usage_tokens,
    uses_native_base_review,
    use_unsafe_windows_wsl_fallback,
    variant_service_tier,
    write_json,
    aggregate_records,
    eligible_variants,
)
from review_costs import refresh_review_cost_report_best_effort


GATE_RUN_LOG_FILENAME = "gate_runs.jsonl"
GATE_SIGNOFF_LOG_FILENAME = "gate_signoffs.jsonl"
GATE_SUMMARY_FILENAME = "gate_summary.json"
GATE_LEADERBOARD_FILENAME = "gate_leaderboard.md"
GATE_TASK_CLASSES = ("phase_gate", "pr_gate")
ARENA_TASK_BY_GATE = {
    "phase_gate": "phase_review",
    "pr_gate": "pr_review",
}
PUBLIC_TASK_BY_GATE = {
    "phase_gate": "review_t2",
    "pr_gate": "review_t4",
}
GATE_SIGNOFF_SCOPE_CHECK = (
    "Before coding from reviewer output, classify each item: valid finding, non-finding suggestion/product preference, "
    "or unclear product decision. Code only valid findings; if advice conflicts with explicit user/product direction, "
    "pause and escalate the tradeoff to the user or parent agent. Focused seam validation can be sufficient to launch "
    "the next review round; full-suite/CI is merge-readiness, not review-launch. Record full-suite/CI as pending, "
    "passed, failed, or intentionally waived/classified, and do not call a PR final/merge-ready while that is unknown."
)
GATE_SIGNOFF_NOTE = "Inspect stored reviewer outputs, then close the gate as clean or findings."
GATE_SIGNOFF_POLICY = "Classify reviewer items before coding; code only valid findings."
GATE_FINDINGS_SCOPE_CHECK = (
    "Findings recorded; no workflow anchor. Classify each item before fixing: valid finding, "
    "non-finding suggestion/product preference, or unclear product decision. Code only valid findings. Focused seam "
    "validation can launch the next review round while full-suite/CI continues as a merge-readiness check; record "
    "full-suite/CI as pending, passed, failed, or intentionally waived/classified before calling the PR merge-ready."
)
INLINE_GATE_FALLBACK_MAX_ATTEMPTS_PER_SLOT = 1


def _print_round_banner(*, gate_task_class: str, round_id: str) -> None:
    print(f"[review-suite] round {PUBLIC_TASK_BY_GATE[gate_task_class]} {round_id}", file=sys.stderr, flush=True)
PROVISIONAL_MIN_MODEL_RUNS = 10
PROVISIONAL_TOTAL_REVIEWER_RUNS = 50
OPERATIONAL_RETRY_DELAY_SECONDS = 1
RETRYABLE_GATE_BLOCK_REASONS = {
    "selected_model_at_capacity",
    "review_timed_out",
    "review_tooling_failure",
    "review_interrupted",
    "missing_reviewer_output",
    "reviewer_process_exited",
}


def _gate_output_refs(runs: list[dict[str, Any]]) -> list[str]:
    refs: list[str] = []
    for run in runs:
        ref = str(run.get("reviewer_output_ref") or "").strip()
        if ref:
            refs.append(ref)
    return refs


@dataclass(frozen=True)
class GateSelection:
    gate_task_class: str
    arena_task_class: str
    mode: str
    variants: tuple[dict[str, Any], ...]
    champion_ids: tuple[str, ...]


def _task_champion_ids(task_state: dict[str, Any]) -> tuple[str, ...]:
    champion_ids = tuple(str(item) for item in list(task_state.get("champion_variant_ids") or []))
    if champion_ids:
        return champion_ids
    legacy = str(task_state.get("champion_variant_id") or "").strip()
    return (legacy,) if legacy else ()


def _gate_retry_delay_seconds(block_reason: str) -> int:
    if block_reason == "selected_model_at_capacity":
        return CAPACITY_RETRY_DELAY_SECONDS
    return OPERATIONAL_RETRY_DELAY_SECONDS


def _gate_runs_path(state_dir: Path) -> Path:
    return state_dir / GATE_RUN_LOG_FILENAME


def _gate_signoffs_path(state_dir: Path) -> Path:
    return state_dir / GATE_SIGNOFF_LOG_FILENAME


def _gate_partial_dir(state_dir: Path) -> Path:
    return state_dir / "gate_partials"


def _gate_summary_path(state_dir: Path) -> Path:
    return state_dir / GATE_SUMMARY_FILENAME


def _gate_leaderboard_path(state_dir: Path) -> Path:
    return state_dir / GATE_LEADERBOARD_FILENAME


def _gate_partial_path(*, state_dir: Path, gate_task_class: str, review_cwd: Path, task_id: str, review_scope: dict[str, str]) -> Path:
    review_token = normalize_review_cwd_value(review_cwd) or str(review_cwd)
    scope_token = json.dumps(review_scope, sort_keys=True)
    digest = blake2s(f"{gate_task_class}:{review_token}:{task_id}:{scope_token}".encode("utf-8"), digest_size=6).hexdigest()
    return _gate_partial_dir(state_dir) / f"{gate_task_class}-{digest}.json"


def _snapshot_queue_item(item: dict[str, Any]) -> dict[str, Any]:
    snapshot = {
        "slot": str(item["slot"]),
        "variant": dict(item["variant"]),
        "retry_attempts": int(item.get("retry_attempts", 0) or 0),
    }
    for key in ("fallback_attempts", "fallback_for_variant_id", "fallback_reason"):
        if item.get(key) is not None:
            snapshot[key] = item[key]
    for key in ("pid", "title", "command", "stdout_path", "stderr_path", "started_at"):
        if item.get(key) is not None:
            value = item[key]
            snapshot[key] = str(value) if isinstance(value, Path) else value
    if item.get("retry_after") is not None:
        snapshot["retry_delay_seconds"] = max(0.0, float(item.get("retry_after") or 0.0) - time.monotonic())
    elif item.get("retry_delay_seconds") is not None:
        snapshot["retry_delay_seconds"] = max(0.0, float(item.get("retry_delay_seconds") or 0.0))
    return snapshot


def _load_gate_partial(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _gate_partial_reference_time(path: Path, payload: dict[str, Any]) -> datetime | None:
    timestamps = [
        _parse_timestamp(str(payload.get(key) or ""))
        for key in ("round_started_at", "recorded_at", "review_completed_at")
    ]
    final_record = payload.get("final_record")
    if isinstance(final_record, dict):
        timestamps.extend(
            _parse_timestamp(str(final_record.get(key) or ""))
            for key in ("round_started_at", "recorded_at", "review_completed_at")
        )
    timestamps = [
        timestamp if timestamp.tzinfo is not None else timestamp.replace(tzinfo=timezone.utc)
        for timestamp in timestamps
        if timestamp is not None
    ]
    if timestamps:
        return max(timestamps)
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _gate_partial_has_live_process(payload: dict[str, Any]) -> bool:
    for key in ("active", "pending", "waiting_retry"):
        for item in list(payload.get(key) or []):
            if _process_is_running(item.get("pid")):
                return True
    return False


def _archive_gate_partial(path: Path, payload: dict[str, Any], *, dismissed_at: str, reason: str) -> Path:
    dismissed_dir = path.parent / "dismissed"
    dismissed_dir.mkdir(parents=True, exist_ok=True)
    base = f"{path.stem}-stale-{dismissed_at.replace('-', '').replace(':', '')}{path.suffix}"
    destination = dismissed_dir / base
    counter = 1
    while destination.exists():
        destination = dismissed_dir / f"{path.stem}-stale-{counter}-{dismissed_at.replace('-', '').replace(':', '')}{path.suffix}"
        counter += 1
    payload = dict(payload)
    payload["status"] = "dismissed"
    payload["dismissed_at"] = dismissed_at
    payload["dismissed_reason"] = reason
    payload["dismissed_previous_path"] = str(path)
    write_json(destination, payload)
    path.unlink(missing_ok=True)
    return destination


def cleanup_stale_gate_partials(
    state_dir: Path,
    *,
    stale_seconds: int = STALE_REVIEW_STATE_TTL_SECONDS,
    preserve_final_paths: set[Path] | None = None,
) -> list[dict[str, Any]]:
    partial_dir = _gate_partial_dir(state_dir)
    if not partial_dir.exists():
        return []
    now = utc_now()
    dismissed_at = now.isoformat().replace("+00:00", "Z")
    reason = f"auto_stale_gate_partial_{max(1, stale_seconds // 3600)}h"
    cleaned: list[dict[str, Any]] = []
    preserved = {path.resolve() for path in preserve_final_paths or set()}
    with state_lock(state_dir, "gate-partial"):
        for path in sorted(partial_dir.glob("*.json")):
            payload = _load_gate_partial(path)
            if not payload:
                continue
            final_record = payload.get("final_record")
            if isinstance(final_record, dict):
                if path.resolve() in preserved:
                    continue
                round_id = str(final_record.get("round_id") or payload.get("round_id") or "")
                if not round_id or not _gate_round_already_recorded(state_dir, round_id):
                    continue
            reference_time = _gate_partial_reference_time(path, payload)
            if reference_time is None or (now - reference_time).total_seconds() < stale_seconds:
                continue
            if _gate_partial_has_live_process(payload):
                continue
            destination = _archive_gate_partial(path, payload, dismissed_at=dismissed_at, reason=reason)
            cleaned.append(
                {
                    "file": path.name,
                    "round_id": str(payload.get("round_id") or ""),
                    "destination": str(destination),
                    "reason": reason,
                }
            )
    return cleaned


def _gate_round_already_recorded(state_dir: Path, round_id: str) -> bool:
    return any(str(record.get("round_id") or "") == round_id for record in read_jsonl(_gate_runs_path(state_dir)))


def load_gate_record(state_dir: Path, round_id: str) -> dict[str, Any] | None:
    for record in read_jsonl(_gate_runs_path(state_dir)):
        if str(record.get("round_id") or "") == round_id:
            return dict(record)
    return None


def gate_signoff_decisions_by_round(state_dir: Path) -> dict[str, dict[str, Any]]:
    decisions: dict[str, dict[str, Any]] = {}
    for decision in read_jsonl(_gate_signoffs_path(state_dir)):
        round_id = str(decision.get("round_id") or "").strip()
        if round_id:
            decisions[round_id] = dict(decision)
    return decisions


def gate_signoff_decision_for_round(state_dir: Path, round_id: str) -> dict[str, Any] | None:
    return gate_signoff_decisions_by_round(state_dir).get(round_id)


def _gate_record_has_blocked_runs(record: dict[str, Any]) -> bool:
    runs = [run for run in list(record.get("runs") or []) if isinstance(run, dict)]
    return bool(runs) and any(bool(run.get("grade_blocked")) for run in runs)


def gate_record_status(record: dict[str, Any], decision: dict[str, Any] | None = None) -> str:
    runs = [run for run in list(record.get("runs") or []) if isinstance(run, dict)]
    if not runs:
        return "unknown"
    if _gate_record_has_blocked_runs(record):
        return "blocked"
    if decision:
        verdict = str(decision.get("verdict") or "").strip()
        if verdict == "clean":
            return "completed"
        if verdict == "findings":
            return "findings"
    if str(record.get("signoff_status") or "").strip() == "pending":
        return "signoff_pending"
    return "completed"


def gate_signoff_action_payload(*, round_id: str, state_dir: Path) -> dict[str, str]:
    script = Path(__file__).resolve().with_name("review_suite_arena.py").as_posix()
    base_command = [sys.executable, script]
    return {
        "lane": "gate-signoff",
        "show_cmd": format_command(
            [
                *base_command,
                "show-round",
                "--round-id",
                round_id,
            ]
        ),
        "clean_cmd": format_command(
            [
                *base_command,
                "close-gate",
                "--round-id",
                round_id,
                "--verdict",
                "clean",
            ]
        ),
        "findings_cmd": format_command(
            [
                *base_command,
                "close-gate",
                "--round-id",
                round_id,
                "--verdict",
                "findings",
            ]
        ),
    }


def record_gate_signoff_decision(
    *,
    state_dir: Path,
    gate_record: dict[str, Any],
    verdict: str,
    note: str | None = None,
    workflow_anchor_recorded: bool = False,
) -> tuple[dict[str, Any], bool]:
    normalized_verdict = str(verdict or "").strip()
    if normalized_verdict not in {"clean", "findings"}:
        raise ValueError("gate signoff verdict must be clean or findings")
    round_id = str(gate_record.get("round_id") or "").strip()
    if not round_id:
        raise ValueError("gate record is missing round_id")
    existing = gate_signoff_decision_for_round(state_dir, round_id)
    if existing:
        existing_verdict = str(existing.get("verdict") or "").strip()
        if existing_verdict != normalized_verdict:
            raise ValueError(f"gate round already closed as {existing_verdict}: {round_id}")
        return existing, False
    decision: dict[str, Any] = {
        "recorded_at": utc_now_iso(),
        "round_id": round_id,
        "task_class": str(gate_record.get("task_class") or ""),
        "task_id": str(gate_record.get("task_id") or ""),
        "verdict": normalized_verdict,
        "review_cwd": str(gate_record.get("review_cwd") or ""),
        "review_cwd_normalized": str(gate_record.get("review_cwd_normalized") or ""),
        "workflow_anchor_recorded": bool(workflow_anchor_recorded),
    }
    if note:
        decision["note"] = str(note)
    with state_lock(state_dir, "gate-signoffs"):
        append_jsonl(_gate_signoffs_path(state_dir), decision)
    return decision, True


def pending_gate_signoff_records(
    *,
    state_dir: Path,
    review_cwd: Path,
    base: str | None = None,
    reviewed_head: str | None = None,
) -> list[dict[str, Any]]:
    normalized_cwd = normalize_review_cwd_value(review_cwd)
    requested_base = str(base or "").strip()
    requested_head = str(reviewed_head or "").strip()
    decisions = gate_signoff_decisions_by_round(state_dir)
    pending: list[dict[str, Any]] = []
    for record in read_jsonl(_gate_runs_path(state_dir)):
        round_id = str(record.get("round_id") or "").strip()
        if not round_id or round_id in decisions:
            continue
        if str(record.get("task_class") or "") not in GATE_TASK_CLASSES:
            continue
        if str(record.get("signoff_status") or "").strip() != "pending":
            continue
        if gate_record_status(dict(record)) != "signoff_pending":
            continue
        record_cwd = str(normalize_record_review_cwd_value(record) or "")
        if record_cwd != normalized_cwd:
            continue
        scope = dict(record.get("review_scope") or {})
        if requested_base and str(scope.get("base") or "").strip() != requested_base:
            continue
        record_head = str(scope.get("reviewed_head") or scope.get("commit_end") or scope.get("commit") or "").strip()
        if requested_head and record_head and record_head != requested_head:
            continue
        pending.append(dict(record))
    return sorted(pending, key=lambda item: str(item.get("recorded_at") or item.get("round_id") or ""))


def _current_branch_name(review_cwd: Path) -> str | None:
    proc = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(review_cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    branch = proc.stdout.strip()
    if not branch or branch == "HEAD":
        return None
    return branch


def _target_label(review_scope: dict[str, str]) -> str:
    explicit = str(review_scope.get("target_label") or "").strip()
    if explicit:
        return explicit
    if review_scope.get("base"):
        return f"base `{review_scope['base']}`"
    if review_scope.get("commit_end"):
        return f"commit range `{review_scope['commit']}..{review_scope['commit_end']}`"
    if review_scope.get("commit"):
        return f"commit `{review_scope['commit']}`"
    return "review target"


def _gate_run_title(*, gate_task_class: str, round_id: str, slot: str, variant_id: str, retry_attempts: int) -> str:
    title = f"review-gate::{gate_task_class}::{round_id}::{slot}::{variant_id}"
    if retry_attempts <= 0:
        return title
    return f"{title}::retry{retry_attempts}"


def _cleanup_paths(run: dict[str, Any]) -> None:
    for key in ("stdout_path", "stderr_path"):
        path = run.get(key)
        if not isinstance(path, Path):
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _timed_out(run: dict[str, Any], *, timeout_seconds: int) -> bool:
    if timeout_seconds <= 0:
        return False
    if bool(run.get("timed_out")):
        return True
    started = float(run.get("started_monotonic") or 0.0)
    if not started:
        return False
    return int(time.monotonic() - started) >= timeout_seconds


def _gate_variant_counts(records: list[dict[str, Any]], gate_task_class: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        if str(record.get("task_class") or "") != gate_task_class:
            continue
        for run in [*list(record.get("retry_runs") or []), *list(record.get("runs") or [])]:
            variant_id = str(run.get("variant_id") or "")
            if not variant_id:
                continue
            counts[variant_id] = counts.get(variant_id, 0) + 1
    return counts


def _arena_public_task_name(gate_task_class: str) -> str:
    return "review_t1" if gate_task_class == "phase_gate" else "review_t3"


def _gate_fallback_variants(
    *,
    roster: dict[str, Any],
    indexed: dict[str, dict[str, Any]],
    gate_task_class: str,
    arena_task_class: str,
    probation_ids: set[str],
    cooling: dict[str, dict[str, Any]],
    state_dir: Path | None = None,
) -> list[dict[str, Any]]:
    fallbacks: list[dict[str, Any]] = []
    configured_gate = gate_config(gate_task_class, state_dir=state_dir)
    for fallback_variant_id in configured_gate.backup_variant_ids:
        fallback = indexed.get(fallback_variant_id)
        if fallback is None:
            continue
        if str(fallback.get("state", "active")) != "active":
            continue
        if arena_task_class not in list(fallback.get("task_classes") or []):
            continue
        fallbacks.append(fallback)
    if not fallbacks:
        fallbacks = [
            variant
            for variant in list(roster.get("variants") or [])
            if str(variant.get("state", "active")) == "active"
            and arena_task_class in list(variant.get("task_classes") or [])
            and str(variant.get("id") or "") not in probation_ids
        ]
    return [variant for variant in fallbacks if str(variant.get("id") or "") not in cooling]


def _inline_gate_fallback_variant(
    *,
    roster: dict[str, Any],
    indexed: dict[str, dict[str, Any]],
    gate_task_class: str,
    arena_task_class: str,
    failed_variant_id: str,
    cooling: dict[str, dict[str, Any]],
    state_dir: Path | None = None,
) -> dict[str, Any] | None:
    probation_ids: set[str] = set()
    for fallback in _gate_fallback_variants(
        roster=roster,
        indexed=indexed,
        gate_task_class=gate_task_class,
        arena_task_class=arena_task_class,
        probation_ids=probation_ids,
        cooling=cooling,
        state_dir=state_dir,
    ):
        if str(fallback.get("id") or "") != failed_variant_id:
            return dict(fallback)
    return None


def _has_prior_gate_record(*, state_dir: Path, gate_task_class: str, review_cwd: Path, task_id: str) -> bool:
    normalized_cwd = normalize_review_cwd_value(review_cwd)
    decisions = gate_signoff_decisions_by_round(state_dir)
    for record in read_jsonl(_gate_runs_path(state_dir)):
        if str(record.get("task_class") or "") != gate_task_class:
            continue
        if str(record.get("task_id") or "") != task_id:
            continue
        if normalized_cwd and normalize_record_review_cwd_value(record) != normalized_cwd:
            continue
        if gate_record_status(dict(record), decisions.get(str(record.get("round_id") or ""))) in {"blocked", "unknown"}:
            continue
        return True
    return False


def _gate_reviewer_count(*, gate_task_class: str, state_dir: Path | None = None, review_cwd: Path | None = None, task_id: str | None = None) -> int:
    configured_gate = gate_config(gate_task_class, state_dir=state_dir)
    default_count = configured_gate.default_reviewer_count
    initial_count = configured_gate.initial_reviewer_count or default_count
    if initial_count == default_count or state_dir is None or review_cwd is None or not task_id:
        return initial_count
    if _has_prior_gate_record(
        state_dir=state_dir,
        gate_task_class=gate_task_class,
        review_cwd=review_cwd,
        task_id=task_id,
    ):
        return default_count
    return initial_count


def _gate_max_active_reviewers(gate_task_class: str, target_count: int, *, state_dir: Path | None = None) -> int:
    configured = gate_config(gate_task_class, state_dir=state_dir).max_active_reviewers
    return min(configured, max(1, target_count))


def _gate_slots(count: int) -> list[str]:
    labels = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]
    if count <= len(labels):
        return labels[:count]
    return labels + [f"reviewer_{idx}" for idx in range(len(labels) + 1, count + 1)]


def _repeat_gate_variant(variant: dict[str, Any], count: int) -> tuple[dict[str, Any], ...]:
    return tuple(variant for _ in range(count))


def _cycle_gate_variants(variants: list[dict[str, Any]], count: int) -> tuple[dict[str, Any], ...]:
    if not variants:
        return ()
    return tuple(variants[idx % len(variants)] for idx in range(count))


def _multi_pass_mode(prefix: str, count: int) -> str:
    return f"{prefix}_double_pass" if count == 2 else f"{prefix}_{count}_pass"


def _gate_configured_primary_variants(
    *,
    indexed: dict[str, dict[str, Any]],
    gate_task_class: str,
    arena_task_class: str,
    cooling: dict[str, dict[str, Any]],
    state_dir: Path | None = None,
) -> tuple[tuple[str, ...], list[dict[str, Any]]]:
    primary_ids = gate_config(gate_task_class, state_dir=state_dir).primary_variant_ids
    variants: list[dict[str, Any]] = []
    for variant_id in primary_ids:
        variant = indexed.get(variant_id)
        if variant is None:
            continue
        if str(variant.get("state", "active")) != "active":
            continue
        if arena_task_class not in list(variant.get("task_classes") or []):
            continue
        if variant_id in cooling:
            continue
        variants.append(variant)
    return primary_ids, variants


def _select_gate_variants(
    *,
    roster: dict[str, Any],
    state_dir: Path,
    gate_task_class: str,
    champion_override: str | None = None,
    reviewer_count: int | None = None,
) -> GateSelection:
    arena_task_class = ARENA_TASK_BY_GATE[gate_task_class]
    reviewer_count = max(1, int(reviewer_count or _gate_reviewer_count(gate_task_class=gate_task_class, state_dir=state_dir)))
    operational_state = load_operational_state(state_dir / OPERATIONAL_STATE_FILENAME)
    task_state = dict(operational_state["task_classes"][arena_task_class] or {})
    champion_ids = _task_champion_ids(task_state)
    probation_ids = {str(item) for item in list(task_state.get("probation_variant_ids") or []) if str(item).strip()}
    indexed = {str(variant["id"]): variant for variant in list(roster.get("variants") or [])}
    cooling = _active_cooldowns(operational_state, arena_task_class)
    champion_override_id = str(champion_override or "").strip()
    if champion_override_id:
        override = indexed.get(champion_override_id)
        if override is None:
            raise ValueError(f"champion override is not in the roster: {champion_override_id}")
        if str(override.get("state", "active")) != "active":
            raise ValueError(f"champion override is not active: {champion_override_id}")
        if arena_task_class not in list(override.get("task_classes") or []):
            raise ValueError(f"champion override is not eligible for {arena_task_class}: {champion_override_id}")
        return GateSelection(
            gate_task_class=gate_task_class,
            arena_task_class=arena_task_class,
            mode=_multi_pass_mode("champion_override", reviewer_count),
            variants=_repeat_gate_variant(override, reviewer_count),
            champion_ids=(champion_override_id,),
        )
    configured_primary_ids, configured_primary_variants = _gate_configured_primary_variants(
        indexed=indexed,
        gate_task_class=gate_task_class,
        arena_task_class=arena_task_class,
        cooling=cooling,
        state_dir=state_dir,
    )
    if configured_primary_variants:
        primary = configured_primary_variants[0]
        return GateSelection(
            gate_task_class=gate_task_class,
            arena_task_class=arena_task_class,
            mode=_multi_pass_mode("configured_primary", reviewer_count),
            variants=_repeat_gate_variant(primary, reviewer_count),
            champion_ids=configured_primary_ids,
        )
    if not champion_ids:
        fallbacks = _gate_fallback_variants(
            roster=roster,
            indexed=indexed,
            gate_task_class=gate_task_class,
            arena_task_class=arena_task_class,
            probation_ids=probation_ids,
            cooling=cooling,
            state_dir=state_dir,
        )
        if not fallbacks:
            configured = ", ".join(gate_config(gate_task_class, state_dir=state_dir).backup_variant_ids)
            raise ValueError(
                f"no configured provisional backups or non-probation supplied-roster fallbacks for {gate_task_class} are active and eligible for {arena_task_class}: {configured}"
            )
        fallback = fallbacks[0]
        return GateSelection(
            gate_task_class=gate_task_class,
            arena_task_class=arena_task_class,
            mode=_multi_pass_mode("provisional_backup", reviewer_count),
            variants=_repeat_gate_variant(fallback, reviewer_count),
            champion_ids=(),
        )
    champions = [
        indexed[variant_id]
        for variant_id in champion_ids
        if variant_id in indexed and str(indexed[variant_id].get("state", "active")) == "active"
    ]
    if not champions:
        raise ValueError(f"configured champions for {gate_task_class} are no longer active in the roster")
    ready_champions = [variant for variant in champions if str(variant["id"]) not in cooling]
    if not ready_champions:
        fallbacks = _gate_fallback_variants(
            roster=roster,
            indexed=indexed,
            gate_task_class=gate_task_class,
            arena_task_class=arena_task_class,
            probation_ids=probation_ids,
            cooling=cooling,
            state_dir=state_dir,
        )
        if not fallbacks:
            cooled = ", ".join(str(variant["id"]) for variant in champions)
            raise ValueError(f"all configured champions for {gate_task_class} are cooling and no fallback is available: {cooled}")
        fallback = fallbacks[0]
        return GateSelection(
            gate_task_class=gate_task_class,
            arena_task_class=arena_task_class,
            mode=_multi_pass_mode("cooldown_backup", reviewer_count),
            variants=_repeat_gate_variant(fallback, reviewer_count),
            champion_ids=champion_ids,
        )
    selection_pool = ready_champions
    if len(selection_pool) == 1:
        champion = selection_pool[0]
        return GateSelection(
            gate_task_class=gate_task_class,
            arena_task_class=arena_task_class,
            mode="double_pass" if reviewer_count == 2 else f"{reviewer_count}_pass",
            variants=_repeat_gate_variant(champion, reviewer_count),
            champion_ids=champion_ids,
        )
    counts = _gate_variant_counts(read_jsonl(_gate_runs_path(state_dir)), gate_task_class)
    champion_order = {variant_id: idx for idx, variant_id in enumerate(champion_ids)}
    ranked = sorted(
        selection_pool,
        key=lambda variant: (
            counts.get(str(variant["id"]), 0),
            champion_order.get(str(variant["id"]), sys.maxsize),
            str(variant["id"]),
        ),
    )
    return GateSelection(
        gate_task_class=gate_task_class,
        arena_task_class=arena_task_class,
        mode="dual_champion" if reviewer_count == 2 else f"multi_champion_{reviewer_count}_pass",
        variants=_cycle_gate_variants(ranked, reviewer_count),
        champion_ids=champion_ids,
    )


def _public_gate_run(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "slot": public_reviewer_label(str(run.get("slot") or "")),
        "status": str(run.get("review_status") or ""),
        "summary": str(run.get("status_summary") or ""),
        "blocked": bool(run.get("grade_blocked")),
        "block": str(run.get("grade_block_reason") or "") or None,
        "ref": str(run.get("reviewer_output_ref") or "") or None,
    }


def _print_live_gate_completed_run(run: dict[str, Any]) -> None:
    write_text(f"{reviewer_output_heading(run)} {reviewer_completion_status(run)}", stream=sys.stderr)


def _record_gate_run(run: dict[str, Any]) -> dict[str, Any]:
    record = {
        "slot": str(run.get("slot") or ""),
        "variant_id": str(run.get("variant_id") or ""),
        "service_tier": str(run.get("service_tier") or "") or None,
        "review_status": str(run.get("review_status") or ""),
        "status_summary": str(run.get("status_summary") or ""),
        "grade_blocked": bool(run.get("grade_blocked")),
        "grade_block_reason": str(run.get("grade_block_reason") or "") or None,
        "elapsed_seconds": run.get("elapsed_seconds"),
        "session_id": str(run.get("session_id") or "") or None,
        "usage": dict(run.get("usage") or {}),
        "tokens_used": run.get("tokens_used"),
        "cost_usd": run.get("cost_usd"),
        "reviewer_output": str(run.get("reviewer_output") or "").strip() or None,
        "reviewer_output_ref": str(run.get("reviewer_output_ref") or "") or None,
    }
    for key in ("cooldown_eligible", "fallback_for_variant_id", "fallback_reason"):
        if run.get(key) is not None:
            record[key] = run[key]
    return record


def summarize_gate_round(
    *,
    gate_task_class: str,
    round_id: str,
    task_id: str,
    mode: str,
    champion_ids: tuple[str, ...],
    review_scope: dict[str, str],
    runs: list[dict[str, Any]],
) -> tuple[dict[str, Any], int]:
    ordered = sorted(runs, key=lambda item: (0 if str(item.get("slot")) == "alpha" else 1, str(item.get("slot"))))
    status = "blocked" if any(bool(run.get("grade_blocked")) for run in ordered) else "signoff_pending"
    payload: dict[str, Any] = {
        "round_id": round_id,
        "task": PUBLIC_TASK_BY_GATE[gate_task_class],
        "status": status,
        "blocked": status == "blocked",
        "runs": [_public_gate_run(run) for run in ordered],
    }
    if status == "signoff_pending":
        payload["signoff_required"] = True
        payload["note"] = GATE_SIGNOFF_NOTE
        payload["policy"] = GATE_SIGNOFF_POLICY
    return payload, 1 if status == "blocked" else 0


def _launch_gate_run(
    *,
    gate_task_class: str,
    round_id: str,
    slot: str,
    variant: dict[str, Any],
    review_cwd: Path,
    review_scope: dict[str, str],
    prompt: str,
    allow_unsafe_windows_wsl_fallback: bool,
    retry_attempts: int,
) -> dict[str, Any]:
    manual_prompt = prompt if not uses_native_base_review(review_scope) else ""
    stdout_tmp = tempfile.NamedTemporaryFile(prefix=f"{gate_task_class}-{slot}-", suffix=".stdout.txt", delete=False)
    stderr_tmp = tempfile.NamedTemporaryFile(prefix=f"{gate_task_class}-{slot}-", suffix=".stderr.txt", delete=False)
    stdout_path = Path(stdout_tmp.name)
    stderr_path = Path(stderr_tmp.name)
    title = _gate_run_title(
        gate_task_class=gate_task_class,
        round_id=round_id,
        slot=slot,
        variant_id=str(variant["id"]),
        retry_attempts=retry_attempts,
    )
    command = build_review_command(
        model=str(variant["model"]),
        reasoning_effort=str(variant["reasoning_effort"]),
        service_tier=variant_service_tier(variant),
        title=title,
        review_scope=review_scope,
        review_cwd=review_cwd,
        prompt=prompt,
        allow_unsafe_windows_wsl_fallback=allow_unsafe_windows_wsl_fallback,
    )
    proc = subprocess.Popen(
        command,
        cwd=str(
            wrapper_launch_cwd()
            if use_unsafe_windows_wsl_fallback(review_cwd, allow_unsafe_windows_wsl_fallback)
            else review_cwd
        ),
        stdin=subprocess.PIPE if manual_prompt else None,
        stdout=stdout_tmp,
        stderr=stderr_tmp,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if manual_prompt and proc.stdin is not None:
        proc.stdin.write(manual_prompt)
        proc.stdin.close()
    stdout_tmp.close()
    stderr_tmp.close()
    return {
        "slot": slot,
        "variant": dict(variant),
        "variant_id": str(variant["id"]),
        "service_tier": variant_service_tier(variant),
        "title": title,
        "command": command,
        "process": proc,
        "pid": proc.pid,
        "started_at": utc_now_iso(),
        "started_monotonic": time.monotonic(),
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
        "retry_attempts": retry_attempts,
        "timed_out": False,
    }


def _reviewer_run_counts(records: list[dict[str, Any]], gate_task_class: str) -> int:
    return sum(
        len(list(record.get("retry_runs") or [])) + len(list(record.get("runs") or []))
        for record in records
        if str(record.get("task_class") or "") == gate_task_class
    )


def aggregate_gate_records(*, state_dir: Path, operational_state: dict[str, Any]) -> dict[str, Any]:
    records = read_jsonl(_gate_runs_path(state_dir))
    summary: dict[str, Any] = {
        "generated_at": utc_now_iso(),
        "report_settings": {
            "provisional_min_model_runs": PROVISIONAL_MIN_MODEL_RUNS,
            "provisional_total_reviewer_runs": PROVISIONAL_TOTAL_REVIEWER_RUNS,
        },
        "task_classes": {},
    }
    for gate_task_class in GATE_TASK_CLASSES:
        arena_task_class = ARENA_TASK_BY_GATE[gate_task_class]
        task_records = [record for record in records if str(record.get("task_class") or "") == gate_task_class]
        metrics: dict[str, dict[str, Any]] = {}
        for record in task_records:
            for run in [*list(record.get("retry_runs") or []), *list(record.get("runs") or [])]:
                variant_id = str(run.get("variant_id") or "")
                if not variant_id:
                    continue
                bucket = metrics.setdefault(
                    variant_id,
                    {
                        "runs": 0,
                        "blocked_runs": 0,
                        "elapsed_values": [],
                        "token_values": [],
                        "cost_values": [],
                    },
                )
                bucket["runs"] += 1
                if bool(run.get("grade_blocked")):
                    bucket["blocked_runs"] += 1
                elapsed_seconds = run.get("elapsed_seconds")
                if isinstance(elapsed_seconds, (int, float)):
                    bucket["elapsed_values"].append(float(elapsed_seconds))
                usage = dict(run.get("usage") or {})
                total_tokens = total_usage_tokens(usage)
                if total_tokens > 0:
                    bucket["token_values"].append(total_tokens)
                cost_usd = run.get("cost_usd")
                if isinstance(cost_usd, (int, float)):
                    bucket["cost_values"].append(float(cost_usd))
        champions = list(gate_config(gate_task_class, state_dir=state_dir).primary_variant_ids) or list(
            _task_champion_ids(dict(operational_state["task_classes"][arena_task_class] or {}))
        )
        for variant_id in champions:
            metrics.setdefault(
                variant_id,
                {
                    "runs": 0,
                    "blocked_runs": 0,
                    "elapsed_values": [],
                    "token_values": [],
                    "cost_values": [],
                },
            )
        leaderboard: list[dict[str, Any]] = []
        for variant_id, bucket in metrics.items():
            runs = int(bucket["runs"])
            leaderboard.append(
                {
                    "variant_id": variant_id,
                    "variant_label": variant_id,
                    "runs": runs,
                    "blocker_pct": round((bucket["blocked_runs"] / runs) * 100.0, 3) if runs else None,
                    "median_elapsed_seconds": round(statistics.median(bucket["elapsed_values"]), 3)
                    if bucket["elapsed_values"]
                    else None,
                    "median_total_tokens": round(statistics.median(bucket["token_values"]), 1)
                    if bucket["token_values"]
                    else None,
                    "median_cost_usd": round(statistics.median(bucket["cost_values"]), 6)
                    if bucket["cost_values"]
                    else None,
                }
            )
        leaderboard.sort(
            key=lambda row: (
                -int(row["runs"]),
                float(row["blocker_pct"]) if row["blocker_pct"] is not None else sys.maxsize,
                str(row["variant_id"]),
            )
        )
        summary["task_classes"][gate_task_class] = {
            "arena_task_class": arena_task_class,
            "champions": champions,
            "rounds": len(task_records),
            "reviewer_runs": _reviewer_run_counts(task_records, gate_task_class),
            "leaderboard": leaderboard,
        }
    return summary


def write_gate_reports(*, state_dir: Path, summary: dict[str, Any]) -> None:
    write_json(_gate_summary_path(state_dir), summary)
    lines = ["# Review Gate Statistics", ""]
    for gate_task_class in GATE_TASK_CLASSES:
        task = dict(summary["task_classes"].get(gate_task_class) or {})
        champions = [str(item) for item in list(task.get("champions") or []) if str(item).strip()]
        lines.append(f"## {PUBLIC_TASK_BY_GATE[gate_task_class]}")
        lines.append("")
        lines.append(f"- Gate source: `{', '.join(champions) if champions else 'provisional backup'}`")
        lines.append(
            f"- Provisional until about `{PROVISIONAL_MIN_MODEL_RUNS}` model runs and `{PROVISIONAL_TOTAL_REVIEWER_RUNS}` total reviewer runs."
        )
        lines.append("")
        lines.append("| reviewer | runs | blocker % | median sec | median tok/run | median cost/run |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for row in list(task.get("leaderboard") or []):
            lines.append(
                f"| {row['variant_label']} | {row['runs']} | {format_decimal(row['blocker_pct'])} | "
                f"{format_decimal(row['median_elapsed_seconds'])} | {format_compact_tokens(row['median_total_tokens'])} | "
                f"{format_cost_cents(row['median_cost_usd'])} |"
            )
        lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append(
        f"- This page is operational reviewer statistics, not a model-quality leaderboard. It is meant to stabilize after about `{PROVISIONAL_MIN_MODEL_RUNS}` runs per model and `{PROVISIONAL_TOTAL_REVIEWER_RUNS}` total reviewer runs per gate class."
    )
    lines.append("")
    _gate_leaderboard_path(state_dir).write_text("\n".join(lines) + "\n", encoding="utf-8")


def refresh_gate_reports(*, state_dir: Path) -> dict[str, Any]:
    operational_state = load_operational_state(state_dir / OPERATIONAL_STATE_FILENAME)
    summary = aggregate_gate_records(state_dir=state_dir, operational_state=operational_state)
    write_gate_reports(state_dir=state_dir, summary=summary)
    return summary


def run_gate_round(
    *,
    gate_task_class: str,
    review_cwd: Path,
    roster_path: Path,
    state_dir: Path,
    sqlite_path: Path,
    task_id: str | None,
    allow_dirty: bool,
    progress_interval_seconds: int,
    timeout_seconds: int,
    allow_unsafe_windows_wsl_fallback: bool,
    review_scope: dict[str, str],
    prompt: str,
    champion_override: str | None = None,
    caller_id: str | None = None,
    caller_id_source: str | None = None,
) -> tuple[dict[str, Any], int]:
    if gate_task_class not in GATE_TASK_CLASSES:
        raise ValueError(f"invalid gate task class: {gate_task_class}")
    if timeout_seconds < 0:
        raise ValueError("gate timeout must be >= 0")
    if review_scope.get("base"):
        ensure_clean_git_worktree(review_cwd, allow_dirty=allow_dirty, review_scope=review_scope)
    roster = load_roster(roster_path)
    resolved_task_id = task_id or _current_branch_name(review_cwd) or _target_label(review_scope)
    partial_path = _gate_partial_path(
        state_dir=state_dir,
        gate_task_class=gate_task_class,
        review_cwd=review_cwd,
        task_id=resolved_task_id,
        review_scope=review_scope,
    )
    cleaned_partials = cleanup_stale_gate_partials(state_dir, preserve_final_paths={partial_path})
    if cleaned_partials:
        print(
            f"[review-suite] archived {len(cleaned_partials)} stale gate partial(s) older than 24h.",
            file=sys.stderr,
            flush=True,
        )
    partial = _load_gate_partial(partial_path)
    if partial and partial.get("final_record"):
        final_record = dict(partial["final_record"])
        if gate_signoff_decision_for_round(state_dir, str(final_record["round_id"])):
            with state_lock(state_dir, "gate-partial"):
                partial_path.unlink(missing_ok=True)
            partial = None
        else:
            payload, exit_code = summarize_gate_round(
                gate_task_class=gate_task_class,
                round_id=str(final_record["round_id"]),
                task_id=str(final_record["task_id"]),
                mode=str(final_record["selection_mode"]),
                champion_ids=tuple(str(item) for item in list(final_record.get("selection_champion_variant_ids") or [])),
                review_scope=dict(final_record.get("review_scope") or {}),
                runs=list(final_record.get("runs") or []),
            )
            if payload.get("status") == "signoff_pending":
                final_record["signoff_status"] = "pending"
                final_record["signoff_required"] = True
                payload["action"] = gate_signoff_action_payload(
                    round_id=str(final_record["round_id"]),
                    state_dir=state_dir,
                )
            with state_lock(state_dir, "gate-runs"), state_lock(state_dir, "gate-reports"):
                if not _gate_round_already_recorded(state_dir, str(final_record["round_id"])):
                    append_jsonl(_gate_runs_path(state_dir), final_record)
                refresh_gate_reports(state_dir=state_dir)
            refresh_review_cost_report_best_effort(state_dir=state_dir, review_cwd=review_cwd)
            with state_lock(state_dir, "gate-partial"):
                partial_path.unlink(missing_ok=True)
            return payload, exit_code
    round_started_at = str(partial.get("round_started_at") or utc_now_iso()) if partial else utc_now_iso()
    if partial:
        override_id = str(champion_override or "").strip()
        if override_id:
            partial_variants = [str(item.get("id") or "") for item in list(partial.get("selection_variants") or [])]
            if any(variant_id != override_id for variant_id in partial_variants):
                raise ValueError(
                    "cannot apply --champion-override to an existing in-flight gate round with different selected reviewers. "
                    "Wait for it, or dismiss the partial round before rerunning."
                )
        selection = GateSelection(
            gate_task_class=gate_task_class,
            arena_task_class=ARENA_TASK_BY_GATE[gate_task_class],
            mode=str(partial["selection_mode"]),
            variants=tuple(dict(item) for item in list(partial.get("selection_variants") or [])),
            champion_ids=tuple(str(item) for item in list(partial.get("selection_champion_variant_ids") or [])),
        )
        round_id = str(partial["round_id"])
    else:
        selection = _select_gate_variants(
            roster=roster,
            state_dir=state_dir,
            gate_task_class=gate_task_class,
            champion_override=champion_override,
            reviewer_count=_gate_reviewer_count(
                gate_task_class=gate_task_class,
                state_dir=state_dir,
                review_cwd=review_cwd,
                task_id=resolved_task_id,
            ),
        )
        round_id = make_round_id(gate_task_class, review_cwd=review_cwd)
    if not selection.variants:
        raise ValueError(f"no gate reviewers selected for {gate_task_class}")
    roster_indexed = {str(variant["id"]): dict(variant) for variant in list(roster.get("variants") or [])}
    indexed = {str(variant["id"]): dict(variant) for variant in selection.variants}
    active: list[dict[str, Any]] = []
    waiting_retry: list[dict[str, Any]] = []
    if partial:
        for item in list(partial.get("waiting_retry") or []):
            queued = dict(item)
            if queued.get("retry_delay_seconds") is not None:
                queued["retry_after"] = time.monotonic() + max(0.0, float(queued.get("retry_delay_seconds") or 0.0))
            waiting_retry.append(queued)
    retry_records: list[dict[str, Any]] = list(partial.get("retry_runs") or []) if partial else []
    completed: list[dict[str, Any]] = list(partial.get("completed_runs") or []) if partial else []
    target_reviewer_count = len(selection.variants)
    max_active_reviewers = _gate_max_active_reviewers(gate_task_class, target_reviewer_count, state_dir=state_dir)
    pending = (
        [dict(item) for item in list(partial.get("pending") or [])]
        + [dict(item) for item in list(partial.get("active") or [])]
        if partial
        else [
            {"slot": slot, "variant": variant, "retry_attempts": 0}
            for slot, variant in zip(_gate_slots(target_reviewer_count), selection.variants)
        ]
    )
    last_progress = time.monotonic()
    last_pending_launch_at: float | None = None
    stall_warned_slots: set[str] = set()
    _print_round_banner(gate_task_class=gate_task_class, round_id=round_id)
    if includes_deep_review_effort([dict(variant) for variant in selection.variants]):
        print_deep_review_wait_note()
    print(
        f"[review-suite] waiting for {target_reviewer_count} gate reviewers; statuses will stream as each reviewer finishes.",
        file=sys.stderr,
        flush=True,
    )

    def persist_partial(final_record: dict[str, Any] | None = None) -> None:
        snapshot = {
            "round_id": round_id,
            "gate_task_class": gate_task_class,
            "task_id": resolved_task_id,
            "round_started_at": round_started_at,
            "review_cwd": str(review_cwd),
            "review_cwd_normalized": normalize_review_cwd_value(review_cwd),
            "caller_id": caller_id,
            "caller_id_source": caller_id_source,
            "review_scope": dict(review_scope),
            "selection_mode": selection.mode,
            "selection_champion_variant_ids": list(selection.champion_ids),
            "selection_variants": [dict(variant) for variant in selection.variants],
            "completed_runs": [_record_gate_run(run) for run in completed],
            "retry_runs": list(retry_records),
            "pending": [_snapshot_queue_item(item) for item in pending],
            "waiting_retry": [_snapshot_queue_item(item) for item in waiting_retry],
            "active": [_snapshot_queue_item(run) for run in active],
        }
        if final_record is not None:
            snapshot["final_record"] = final_record
        with state_lock(state_dir, "gate-partial"):
            write_json(partial_path, snapshot)

    if not partial:
        persist_partial()

    def _queue_to_active(queued: dict[str, Any]) -> dict[str, Any]:
        launched = _launch_gate_run(
            gate_task_class=gate_task_class,
            round_id=round_id,
            slot=str(queued["slot"]),
            variant=dict(queued["variant"]),
            review_cwd=review_cwd,
            review_scope=review_scope,
            prompt=prompt,
            allow_unsafe_windows_wsl_fallback=allow_unsafe_windows_wsl_fallback,
            retry_attempts=int(queued.get("retry_attempts", 0) or 0),
        )
        for key in ("fallback_attempts", "fallback_for_variant_id", "fallback_reason"):
            if queued.get(key) is not None:
                launched[key] = queued[key]
        return launched

    def launch_ready(now: float) -> None:
        nonlocal last_pending_launch_at, last_progress
        while waiting_retry and len(active) < max_active_reviewers:
            ready = [run for run in waiting_retry if float(run.get("retry_after") or 0.0) <= now]
            if not ready:
                break
            queued = ready[0]
            waiting_retry.remove(queued)
            active.append(_queue_to_active(queued))
            last_progress = time.monotonic()
            persist_partial()
        while pending and len(active) < max_active_reviewers:
            if not pending_launch_ready(
                dispatch_stagger_seconds=MULTI_REVIEW_DISPATCH_STAGGER_SECONDS,
                last_pending_launch_at=last_pending_launch_at,
                now=now,
            ):
                break
            queued = pending.pop(0)
            active.append(_queue_to_active(queued))
            last_pending_launch_at = now
            last_progress = time.monotonic()
            persist_partial()

    while len(completed) < target_reviewer_count:
        now = time.monotonic()
        launch_ready(now)
        for run in list(active):
            proc = run["process"]
            assert isinstance(proc, subprocess.Popen)
            if proc.poll() is None and _timed_out(run, timeout_seconds=timeout_seconds):
                proc.kill()
                run["timed_out"] = True
            if proc.poll() is None:
                stall_reason = _transport_stalled(run)
                if stall_reason is not None:
                    label = public_reviewer_label(str(run.get("slot") or "reviewer"))
                    print(
                        f"[review-suite] {label} transport stalled ({stall_reason}); stopping this reviewer and preserving the other reviewer output.",
                        file=sys.stderr,
                        flush=True,
                    )
                    _terminate_process_tree(run.get("pid"))
                    run["transport_stalled"] = True
            if proc.poll() is None:
                hang_reason = _transport_hung_after_output(run)
                if hang_reason is not None:
                    label = public_reviewer_label(str(run.get("slot") or "reviewer"))
                    print(
                        f"[review-suite] {label} transport hung after output ({hang_reason}); stopping process and preserving captured output.",
                        file=sys.stderr,
                        flush=True,
                    )
                    _terminate_process_tree(run.get("pid"))
            if proc.poll() is None:
                continue
            proc.wait()
            variant = dict(run["variant"])
            capture = collect_completed_review_capture(
                slot=str(run["slot"]),
                variant_id=str(run["variant_id"]),
                variant=variant,
                title=str(run["title"]),
                command=list(run.get("command") or []),
                stdout_path=Path(run["stdout_path"]),
                stderr_path=Path(run["stderr_path"]),
                started_at=str(run.get("started_at") or "") or None,
                sqlite_path=sqlite_path,
                review_cwd=review_cwd,
                timed_out=bool(run.get("timed_out")),
                transport_stalled=bool(run.get("transport_stalled")),
            )
            _cleanup_paths(run)
            active.remove(run)
            block_reason = str(capture.get("grade_block_reason") or "")
            if block_reason in RETRYABLE_GATE_BLOCK_REASONS and int(run.get("retry_attempts", 0) or 0) < CAPACITY_RETRY_MAX_ATTEMPTS:
                retry_attempts = int(run.get("retry_attempts", 0) or 0) + 1
                retry_delay_seconds = _gate_retry_delay_seconds(block_reason)
                print(
                    f"[review-suite] {public_reviewer_label(str(run['slot']))} hit {block_reason}; retrying in "
                    f"{retry_delay_seconds}s (attempt {retry_attempts}/{CAPACITY_RETRY_MAX_ATTEMPTS})",
                    file=sys.stderr,
                    flush=True,
                )
                retry_records.append(_record_gate_run(capture))
                waiting_retry.append(
                    {
                        "slot": run["slot"],
                        "variant": variant,
                        "retry_attempts": retry_attempts,
                        "retry_after": time.monotonic() + retry_delay_seconds,
                    }
                )
                persist_partial()
                continue
            if (
                block_reason in RETRYABLE_GATE_BLOCK_REASONS
                and int(run.get("fallback_attempts", 0) or 0) < INLINE_GATE_FALLBACK_MAX_ATTEMPTS_PER_SLOT
            ):
                operational_state = load_operational_state(state_dir / OPERATIONAL_STATE_FILENAME)
                fallback = _inline_gate_fallback_variant(
                    roster=roster,
                    indexed=roster_indexed,
                    gate_task_class=gate_task_class,
                    arena_task_class=selection.arena_task_class,
                    failed_variant_id=str(variant.get("id") or ""),
                    cooling=_active_cooldowns(operational_state, selection.arena_task_class),
                    state_dir=state_dir,
                )
                if fallback is not None:
                    fallback_attempts = int(run.get("fallback_attempts", 0) or 0) + 1
                    capture["cooldown_eligible"] = True
                    retry_records.append(_record_gate_run(capture))
                    print(
                        f"[review-suite] {public_reviewer_label(str(run['slot']))} exhausted retries after {block_reason}; "
                        f"switching this slot to a cooldown-aware fallback reviewer.",
                        file=sys.stderr,
                        flush=True,
                    )
                    waiting_retry.append(
                        {
                            "slot": run["slot"],
                            "variant": fallback,
                            "retry_attempts": 0,
                            "retry_after": time.monotonic() + OPERATIONAL_RETRY_DELAY_SECONDS,
                            "fallback_attempts": fallback_attempts,
                            "fallback_for_variant_id": str(variant.get("id") or ""),
                            "fallback_reason": block_reason,
                        }
                    )
                    persist_partial()
                    continue
            completed.append(capture)
            persist_partial()
            _print_live_gate_completed_run(capture)
        if len(completed) >= target_reviewer_count:
            break
        now = time.monotonic()
        if active and now - last_progress >= progress_interval_seconds:
            _print_transport_events(active)
            _print_stall_warnings(
                active_runs=active,
                indexed=indexed,
                sqlite_path=sqlite_path,
                review_cwd=review_cwd,
                warned_slots=stall_warned_slots,
            )
            print(_progress_status_line(active), file=sys.stderr, flush=True)
            last_progress = now
        time.sleep(1.0)

    payload, exit_code = summarize_gate_round(
        gate_task_class=gate_task_class,
        round_id=round_id,
        task_id=resolved_task_id,
        mode=selection.mode,
        champion_ids=selection.champion_ids,
        review_scope=review_scope,
        runs=completed,
    )
    record_runs = [_record_gate_run(run) for run in completed]
    cooldown_updates = _apply_capacity_cooldowns(
        state_dir=state_dir,
        round_payload={
            "task_class": selection.arena_task_class,
            "runs": [
                *[dict(run) for run in retry_records if bool(dict(run).get("cooldown_eligible"))],
                *record_runs,
            ],
        },
    )
    if cooldown_updates:
        payload["cooldowns"] = [
            {
                "variant": str(update.get("variant_id") or ""),
                "reason": str(update.get("reason") or ""),
                "until": format_cooldown_until_for_display(update.get("until")),
            }
            for update in cooldown_updates
        ]
    if payload.get("status") == "signoff_pending":
        payload["action"] = gate_signoff_action_payload(round_id=round_id, state_dir=state_dir)
    review_completed_at = utc_now_iso()
    record = {
        "recorded_at": review_completed_at,
        "round_id": round_id,
        "task_class": gate_task_class,
        "arena_task_class": selection.arena_task_class,
        "task_id": resolved_task_id,
        "round_started_at": round_started_at,
        "review_completed_at": review_completed_at,
        "selection_mode": selection.mode,
        "selection_champion_variant_ids": list(selection.champion_ids),
        "review_cwd": str(review_cwd),
        "review_cwd_normalized": normalize_review_cwd_value(review_cwd),
        "caller_id": caller_id,
        "caller_id_source": caller_id_source,
        "review_scope": dict(review_scope),
        "signoff_status": "pending" if payload.get("status") == "signoff_pending" else "blocked",
        "signoff_required": payload.get("status") == "signoff_pending",
        "retry_runs": retry_records,
        "runs": record_runs,
    }
    if cooldown_updates:
        record["cooldown_updates"] = cooldown_updates
    persist_partial(final_record=record)
    with state_lock(state_dir, "gate-runs"), state_lock(state_dir, "gate-reports"):
        if not _gate_round_already_recorded(state_dir, str(record["round_id"])):
            append_jsonl(_gate_runs_path(state_dir), record)
            refresh_gate_reports(state_dir=state_dir)
        else:
            refresh_gate_reports(state_dir=state_dir)
    refresh_review_cost_report_best_effort(state_dir=state_dir, review_cwd=review_cwd)
    with state_lock(state_dir, "gate-partial"):
        partial_path.unlink(missing_ok=True)
    return payload, exit_code
