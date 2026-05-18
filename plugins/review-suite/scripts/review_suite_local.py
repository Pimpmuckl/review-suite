from __future__ import annotations

import json
import math
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import blake2s
from pathlib import Path
from typing import Any, Callable

from rollout_capture import (
    DEFAULT_SQLITE_STATE_PATH,
    REVIEW_SUBAGENT_SOURCE,
    enrich_thread_record,
    find_review_child_thread,
    find_thread_by_id,
    find_thread_by_title,
    rollout_activity_summary,
)
from review_suite_core import (
    dirty_worktree_scope,
    format_command,
    inspect_workflow_status,
    meaningful_worktree_status_entries,
    normalize_cwd,
    use_unsafe_windows_wsl_fallback,
    utc_now,
    utc_now_iso,
    validate_codex_runtime,
    wrapper_launch_cwd,
    write_text,
)

TASK_CLASSES = ("phase_review", "pr_review")
PAIR_SELECTION_MODES = ("legacy", "slight_bias", "true_scramble")
LOCAL_REVIEW_LANE_STAGE_RANK = {
    "review_t1": 1,
    "review_t2": 2,
    "review_t3": 3,
    "review_t4": 4,
}
MANUAL_REVIEW_PROMPT_MAX_CHARS = 600_000
STALE_REVIEW_STATE_TTL_SECONDS = 24 * 60 * 60
RUN_LOG_FILENAME = "runs.jsonl"
SUMMARY_FILENAME = "summary.json"
ROSTER_FILENAME = "roster.json"
RUBRIC_FILENAME = "rubric_v2.json"
OPERATIONAL_STATE_FILENAME = "operational_state.json"
ROUNDS_DIRNAME = "rounds"
GRADE_SCHEMA = "winner_basis_v1"
LEGACY_GRADE_BASIS = "legacy_score_grade"
GRADE_BASIS_VALUES = (
    "valid_findings_vs_none",
    "more_valid_findings",
    "better_finding_validity",
    "better_bug_coverage",
    "false_positive_loss",
    "hallucinated_finding_loss",
    "fringe_finding_loss",
    "tie_clean",
    "tie_both_useful",
)
GARBAGE_FINDING_LOSS_BASES = {"false_positive_loss", "hallucinated_finding_loss", "fringe_finding_loss"}
LOW_QUALITY_LOSS_BASES = GARBAGE_FINDING_LOSS_BASES | {"better_finding_validity"}
MISSED_BUG_LOSS_BASES = {"valid_findings_vs_none", "more_valid_findings", "better_bug_coverage"}
VALID_FINDING_WIN_BASES = {"valid_findings_vs_none", "more_valid_findings", "better_finding_validity", "better_bug_coverage"}
BUG_OPPORTUNITY_BASES = VALID_FINDING_WIN_BASES | {"tie_both_useful"}
BOTH_REVIEWERS_FOUND_BASES = {"more_valid_findings", "better_bug_coverage", "tie_both_useful"}
PUBLIC_REVIEWER_LABELS = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot")
CAPACITY_COOLDOWN_SECONDS = (30 * 60, 2 * 60 * 60, 6 * 60 * 60, 12 * 60 * 60)
CAPACITY_RETRY_DELAY_SECONDS = 10
CAPACITY_RETRY_MAX_ATTEMPTS = 1
MULTI_REVIEW_DISPATCH_STAGGER_SECONDS = 5.0
DEEP_REVIEW_EFFORTS = {"high", "xhigh"}
SUPPORTED_CODEX_SERVICE_TIERS = {"fast", "flex"}
REVIEW_STALL_WARNING_SECONDS = 10 * 60
TRANSPORT_STALL_GRACE_SECONDS = 3 * 60
TRANSPORT_RECONNECT_PATTERNS = (
    "stream disconnected",
    "ERROR: Reconnecting...",
    "falling back to HTTP",
)
CALLER_ID_ENV_KEYS = (
    ("PWF_SUBAGENT_ID", "pwf_subagent_id"),
    ("CODEX_THREAD_ID", "codex_thread_id"),
)


@dataclass(frozen=True)
class LocalReviewRequest:
    review_scope: dict[str, Any]
    prompt: str
    target_label: str


def pending_launch_ready(
    *,
    dispatch_stagger_seconds: float,
    last_pending_launch_at: float | None,
    now: float,
) -> bool:
    if dispatch_stagger_seconds <= 0:
        return True
    if last_pending_launch_at is None:
        return True
    return (now - last_pending_launch_at) >= dispatch_stagger_seconds


def includes_deep_review_effort(items: list[dict[str, Any]]) -> bool:
    return any(str(item.get("reasoning_effort") or "").strip().lower() in DEEP_REVIEW_EFFORTS for item in items)


def print_deep_review_wait_note() -> None:
    print(
        "[review-suite] deep review selected; reviews can take up to 20m. Wait for wrapper output; do not inspect state files/logs or rerun unless the wrapper exits.",
        file=sys.stderr,
        flush=True,
    )


def load_custom_instructions(*, instructions: str | None, instructions_file: str | None) -> str | None:
    if instructions is not None and instructions_file is not None:
        raise ValueError("use either --instructions or --instructions-file")
    if instructions_file is not None:
        try:
            payload = Path(instructions_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(str(exc)) from exc
    elif instructions is not None:
        payload = instructions
    else:
        return None
    if not payload.strip():
        raise ValueError("custom instructions must not be empty")
    return payload


def normalize_commit_spec(commit_values: list[str] | None) -> tuple[str | None, str | None]:
    if not commit_values:
        return None, None
    if len(commit_values) == 1:
        return commit_values[0], None
    if len(commit_values) == 2:
        return commit_values[0], commit_values[1]
    raise ValueError("--commit accepts one sha or two shas")


def build_phase_instructions(target_label: str) -> str:
    return (
        "Review this implementation slice for correctness and regression risk.\n"
        f"The review target is {target_label}.\n"
        f"{build_correctness_review_contract()}"
    )


def build_pr_instructions(target_label: str) -> str:
    return (
        "Review this PR-ready branch diff for correctness and regression risk.\n"
        f"The review target is {target_label}.\n"
        f"{build_correctness_review_contract()}"
    )


def build_correctness_review_contract() -> str:
    return (
        "Reviewer output is advisory risk input, not authoritative product direction.\n"
        "Return every concrete finding you can support; do not stop after the first issue.\n"
        "For each finding, include severity, file path and line number when available, violated invariant or owner/source of truth when clear, and fix suggestion.\n"
        "A finding is only valid when it identifies a concrete correctness, regression, integration, security, accessibility, or maintainability risk against stated requirements, docs, code invariants, or explicit contracts.\n"
        "Do not treat UX preference, product-scope speculation, backwards-compat speculation, or alternative product direction as a blocking finding.\n"
        "Do not assume backwards compatibility, legacy behavior, broad fallback behavior, or support for unsupported inputs unless the task, docs, or diff explicitly requires it.\n"
        "If a concern depends on ambiguous product scope or conflicts with explicit user/product direction, report it under `Scope questions / suggestions (non-findings)` with the tradeoff to escalate instead of assigning severity.\n"
        "Do not recommend code changes that reverse explicit product intent.\n"
        "When discussing validation, distinguish focused seam validation from full-suite/CI validation: focused review-relevant checks can be enough to launch the next review round, while full-suite/CI remains a merge-readiness requirement.\n"
        "Record full-suite/CI validation status when relevant as pending, passed, failed, or intentionally waived/classified; do not call a PR final or merge-ready while that status is unknown.\n"
        "When applicable, flag correctness-relevant risks from oversized or hard-to-stage diffs, external integration surface breaks, missing regression or integration coverage, and unbounded agent-context injection.\n"
        "Skip style-only comments. If there are no issues, say 'No findings.'\n"
        "Do not suggest localized guards when the evidence points to broader ownership, fallback, retry, lifecycle, concurrency, or persistence issues."
    )


def uses_native_base_review(review_scope: dict[str, Any]) -> bool:
    return bool(str(review_scope.get("base") or "").strip()) and not bool(review_scope.get("manual_prompt_mode"))


def _run_git_artifact(review_cwd: Path, args: list[str], *, default_error: str) -> str:
    proc = subprocess.run(
        args,
        cwd=str(review_cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise ValueError((proc.stderr or proc.stdout or "").strip() or default_error)
    return proc.stdout


def _current_reviewed_head(review_cwd: Path) -> str:
    return _run_git_artifact(
        review_cwd,
        ["git", "rev-parse", "HEAD"],
        default_error="git rev-parse HEAD failed",
    ).strip()


def _current_merge_base(review_cwd: Path, base: str) -> str:
    merge_base = _run_git_artifact(
        review_cwd,
        ["git", "merge-base", base, "HEAD"],
        default_error=f"git merge-base {base} HEAD failed",
    ).strip()
    if not merge_base:
        raise ValueError(f"git merge-base {base} HEAD returned no merge base")
    return merge_base


def _commit_review_artifact(*, review_cwd: Path, commit: str, commit_end: str | None) -> tuple[str, dict[str, Any], str]:
    if commit_end:
        return (
            _run_git_artifact(
                review_cwd,
                ["git", "diff", "--find-renames", "--stat", "--patch", f"{commit}..{commit_end}"],
                default_error=f"git diff {commit}..{commit_end} failed",
            ),
            {
                "commit": commit,
                "commit_end": commit_end,
                "target_label": f"commit range `{commit}..{commit_end}`",
            },
            f"commit range `{commit}..{commit_end}`",
        )
    return (
        _run_git_artifact(
            review_cwd,
            ["git", "show", "--stat", "--patch", "--find-renames", commit],
            default_error=f"git show {commit} failed",
        ),
        {"commit": commit, "target_label": f"commit `{commit}`"},
        f"commit `{commit}`",
    )


def _base_branch_review_artifact(*, review_cwd: Path, base: str) -> tuple[str, dict[str, Any], str]:
    merge_base = _current_merge_base(review_cwd, base)
    reviewed_head = _current_reviewed_head(review_cwd)
    target_label = f"branch diff against base `{base}`"
    return (
        _run_git_artifact(
            review_cwd,
            ["git", "diff", "--find-renames", "--stat", "--patch", f"{merge_base}..HEAD"],
            default_error=f"git diff {merge_base}..HEAD failed",
        ),
        {
            "base": base,
            "manual_prompt_mode": True,
            "merge_base": merge_base,
            "reviewed_head": reviewed_head,
            "target_label": target_label,
        },
        target_label,
    )


def _combined_review_instructions(*, standard_instructions: str, custom_instructions: str | None) -> str:
    instruction_text = standard_instructions.strip()
    if not instruction_text:
        raise ValueError("manual review mode requires built-in instructions")
    if custom_instructions is None:
        return instruction_text
    return f"{instruction_text}\n\nAdditional review instructions:\n{custom_instructions}"


def _manual_prompt_too_large_message(
    *,
    prompt: str,
    review_scope: dict[str, Any],
    dirty_scope: dict[str, Any] | None = None,
) -> str:
    reason = str(review_scope.get("manual_prompt_reason") or "manual_review_required").strip()
    message = (
        f"manual review artifact is too large for reliable Codex review launch "
        f"({len(prompt)} chars; limit {MANUAL_REVIEW_PROMPT_MAX_CHARS})."
    )
    if reason == "dirty_worktree_outside_branch_diff":
        paths = list((dirty_scope or {}).get("unrelated_dirty_paths") or [])
        preview = ", ".join(str(path) for path in paths[:3])
        suffix = f" Examples: {preview}." if preview else ""
        return (
            f"{message} Dirty worktree paths outside the committed branch diff were included.{suffix} "
            "Clean, stash, or commit unrelated dirty files, then rerun without --allow-dirty."
        )
    return (
        f"{message} Split the change into a smaller review slice, or remove custom instructions."
    )


def _ensure_manual_prompt_within_limit(
    *,
    prompt: str,
    review_scope: dict[str, Any],
    dirty_scope: dict[str, Any] | None = None,
) -> None:
    if len(prompt) <= MANUAL_REVIEW_PROMPT_MAX_CHARS:
        return
    raise ValueError(
        _manual_prompt_too_large_message(
            prompt=prompt,
            review_scope=review_scope,
            dirty_scope=dirty_scope,
        )
    )


def _manual_review_request(
    *,
    review_scope: dict[str, Any],
    target_label: str,
    instructions: str,
    diff_text: str,
    dirty_scope: dict[str, Any] | None = None,
) -> LocalReviewRequest:
    prompt = build_manual_review_prompt(
        instructions=instructions,
        diff_text=diff_text,
    )
    _ensure_manual_prompt_within_limit(
        prompt=prompt,
        review_scope=review_scope,
        dirty_scope=dirty_scope,
    )
    return LocalReviewRequest(
        review_scope=review_scope,
        prompt=prompt,
        target_label=target_label,
    )


def build_local_review_request(
    *,
    review_cwd: Path,
    base: str,
    commit_values: list[str] | None,
    instruction_builder: Callable[[str], str],
    custom_instructions: str | None,
) -> LocalReviewRequest:
    commit, commit_end = normalize_commit_spec(commit_values)
    if commit:
        if base != "main":
            raise ValueError("use either --base or --commit")
        diff_text, review_scope, target_label = _commit_review_artifact(
            review_cwd=review_cwd,
            commit=commit,
            commit_end=commit_end,
        )
        return _manual_review_request(
            review_scope=review_scope,
            target_label=target_label,
            instructions=_combined_review_instructions(
                standard_instructions=instruction_builder(target_label),
                custom_instructions=custom_instructions,
            ),
            diff_text=diff_text,
        )
    if custom_instructions is None:
        base_dirty_scope = dirty_worktree_scope(review_cwd, str(base))
        native_scope = {
            "base": str(base),
            "merge_base": _current_merge_base(review_cwd, str(base)),
            "reviewed_head": _current_reviewed_head(review_cwd),
            "target_label": f"base `{base}`",
        }
        if bool(base_dirty_scope.get("all_dirty_paths_outside_branch_diff")):
            unrelated_paths = list(base_dirty_scope.get("unrelated_dirty_paths") or [])
            native_scope["ignored_dirty_path_count"] = len(unrelated_paths)
            native_scope["ignored_dirty_paths"] = unrelated_paths[:3]
        return LocalReviewRequest(review_scope=native_scope, prompt="", target_label=str(native_scope["target_label"]))
    diff_text, review_scope, target_label = _base_branch_review_artifact(review_cwd=review_cwd, base=str(base))
    return _manual_review_request(
        review_scope=review_scope,
        target_label=target_label,
        instructions=_combined_review_instructions(
            standard_instructions=instruction_builder(target_label),
            custom_instructions=custom_instructions,
        ),
        diff_text=diff_text,
    )


def _is_branch_review_scope(review_scope: dict[str, Any]) -> bool:
    if not str(review_scope.get("base") or "").strip():
        return False
    if str(review_scope.get("commit") or "").strip():
        return False
    if str(review_scope.get("commit_end") or "").strip():
        return False
    return True


def _review_state_status_command(*, review_cwd: Path, base: str, state_dir: Path) -> str:
    return format_command(
        [
            sys.executable,
            Path(__file__).resolve().with_name("review_state.py").as_posix(),
            "status",
            "--cd",
            str(review_cwd),
            "--base",
            str(base),
            "--state-dir",
            str(state_dir),
        ]
    )


def guard_branch_signoff_lane(
    *,
    lane: str,
    review_cwd: Path,
    base: str,
    state_dir: Path,
    review_scope: dict[str, Any],
) -> None:
    if not _is_branch_review_scope(review_scope):
        return
    try:
        status = inspect_workflow_status(
            state_dir=state_dir,
            review_cwd=review_cwd,
            base=str(base),
        )
    except ValueError:
        return
    recommendation = str(status.get("recommendation") or "").strip()
    if recommendation not in {"review-followup", "coherence-review", "full-review"}:
        return
    recommended_lane = str(status.get("recommended_lane") or "").strip()
    if recommended_lane == lane and recommendation in {"coherence-review", "full-review"}:
        return
    note = str(status.get("note") or "").strip()
    command = _review_state_status_command(
        review_cwd=review_cwd,
        base=str(base),
        state_dir=state_dir,
    )
    if recommendation == "review-followup":
        raise ValueError(
            f"{lane} only signs off the current reviewed head. This branch has moved since the last valid review anchor and now requires review-followup instead. "
            f"{note} Run {command} and follow its action before another signoff pass."
        )
    if recommendation == "coherence-review":
        raise ValueError(
            f"{lane} only signs off a branch that is still aligned with the last reviewed head. The current post-review delta is too large for signoff and needs a fresh coherence/full-diff pass first. "
            f"{note} Run {command} and follow its action before another signoff pass."
        )
    raise ValueError(
        f"{lane} only signs off a branch that is still aligned with the last reviewed head. The current branch state needs a fresh full review before signoff. "
        f"{note} Run {command} and follow its action before another signoff pass."
    )


def guard_no_stage_step_down(
    *,
    lane: str,
    review_cwd: Path,
    base: str,
    state_dir: Path,
    review_scope: dict[str, Any],
) -> None:
    if not _is_branch_review_scope(review_scope):
        return
    lane_rank = LOCAL_REVIEW_LANE_STAGE_RANK.get(lane)
    if lane_rank is None:
        return
    try:
        status = inspect_workflow_status(
            state_dir=state_dir,
            review_cwd=review_cwd,
            base=str(base),
        )
    except ValueError:
        return
    current_stage_lane = str(status.get("current_stage_lane") or status.get("recommended_lane") or status.get("last_reviewed_lane") or "").strip()
    current_stage_rank = LOCAL_REVIEW_LANE_STAGE_RANK.get(current_stage_lane)
    if current_stage_rank is None or lane_rank >= current_stage_rank:
        return
    command = _review_state_status_command(
        review_cwd=review_cwd,
        base=str(base),
        state_dir=state_dir,
    )
    raise ValueError(
        f"{lane} would step down from the current review stage {current_stage_lane}. "
        "Review-suite lanes are monotonic for a branch: once a branch reaches a higher tier, do not rerun lower tiers after amended commits. "
        f"Run {command} and follow its action. Do not invent a lower-tier final-head requirement. "
        "Only pass --allow-stage-step-down when the user/operator explicitly requests an exceptional lower-tier rerun."
    )


def script_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_roster_path() -> Path:
    return script_root() / "references" / ROSTER_FILENAME


def default_rubric_path() -> Path:
    return script_root() / "references" / RUBRIC_FILENAME


def default_state_dir() -> Path:
    return ensure_state_dir()

def ensure_state_dir() -> Path:
    canonical = Path.home() / ".codex" / "state" / "review-suite"
    canonical.mkdir(parents=True, exist_ok=True)
    return canonical


def default_operational_state() -> dict[str, Any]:
    return {
        "generated_at": utc_now_iso(),
        "task_classes": {
            task_class: {
                "champion_variant_id": None,
                "champion_variant_ids": [],
                "probation_variant_ids": [],
                "stable_variant_ids": [],
                "cooldowns": {},
            }
            for task_class in TASK_CLASSES
        },
    }


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, path)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    text = ""
    if rows:
        text = "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n"
    _atomic_write_text(path, text)


@contextmanager
def state_lock(state_dir: Path, name: str, *, timeout_seconds: int = 30, poll_seconds: float = 0.1):
    locks_dir = state_dir / ".locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    lock_path = locks_dir / f"{name}.lock"
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            lock_path.mkdir()
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for lock: {lock_path}")
            time.sleep(poll_seconds)
    try:
        yield
    finally:
        try:
            lock_path.rmdir()
        except OSError:
            pass


def load_roster(path: Path) -> dict[str, Any]:
    roster = read_json(path)
    ids = set()
    for variant in roster.get("variants", []):
        variant_id = variant["id"]
        if variant_id in ids:
            raise ValueError(f"duplicate variant id: {variant_id}")
        ids.add(variant_id)
        state = variant.get("state", "active")
        if state not in {"active", "retired"}:
            raise ValueError(f"variant {variant_id} has invalid state: {state}")
        for task_class in variant.get("task_classes", []):
            if task_class not in TASK_CLASSES:
                raise ValueError(f"variant {variant_id} has invalid task_class: {task_class}")
    return roster


def load_rubric(path: Path) -> dict[str, Any]:
    rubric = read_json(path)
    expected = list(GRADE_BASIS_VALUES)
    if rubric.get("basis") != expected:
        raise ValueError(f"rubric basis must be {expected}")
    return rubric


def load_operational_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return default_operational_state()
    payload = read_json(path)
    payload.setdefault("task_classes", {})
    for task_class in TASK_CLASSES:
        payload["task_classes"].setdefault(
            task_class,
            {
                "champion_variant_id": None,
                "champion_variant_ids": [],
                "probation_variant_ids": [],
                "stable_variant_ids": [],
                "cooldowns": {},
            },
        )
        payload["task_classes"][task_class].setdefault("cooldowns", {})
        payload["task_classes"][task_class].setdefault("champion_variant_ids", [])
        payload["task_classes"][task_class].setdefault("probation_variant_ids", [])
    _prune_expired_cooldowns(payload)
    return payload


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _payload_reference_time(payload: dict[str, Any]) -> datetime | None:
    timestamps = [
        _parse_timestamp(str(payload.get(key) or ""))
        for key in ("review_completed_at", "review_started_at", "sampled_at", "recorded_at", "round_started_at")
    ]
    timestamps = [
        timestamp if timestamp.tzinfo is not None else timestamp.replace(tzinfo=timezone.utc)
        for timestamp in timestamps
        if timestamp is not None
    ]
    if timestamps:
        return max(timestamps)
    file_path = str(payload.get("_round_file_path") or "").strip()
    if file_path:
        try:
            return datetime.fromtimestamp(Path(file_path).stat().st_mtime, tz=timezone.utc)
        except OSError:
            pass
    return None


def format_cooldown_until_for_display(value: object) -> str:
    timestamp = _parse_timestamp(str(value or ""))
    if timestamp is None:
        return str(value or "")
    return timestamp.astimezone().isoformat(timespec="seconds")


def _prune_expired_cooldowns(payload: dict[str, Any], *, now: datetime | None = None) -> None:
    now = now or utc_now()
    for task_class in TASK_CLASSES:
        slot = payload["task_classes"].setdefault(task_class, {})
        cooldowns = dict(slot.get("cooldowns") or {})
        active: dict[str, Any] = {}
        for variant_id, entry in cooldowns.items():
            until = _parse_timestamp(str((entry or {}).get("until") or ""))
            if until and until > now:
                active[str(variant_id)] = {
                    "until": until.isoformat().replace("+00:00", "Z"),
                    "failure_count": int((entry or {}).get("failure_count", 1) or 1),
                    "last_reason": str((entry or {}).get("last_reason") or "selected_model_at_capacity"),
                    "last_triggered_at": str((entry or {}).get("last_triggered_at") or ""),
                }
        slot["cooldowns"] = active


def _active_cooldowns(operational_state: dict[str, Any], task_class: str) -> dict[str, dict[str, Any]]:
    _prune_expired_cooldowns(operational_state)
    return dict(operational_state["task_classes"][task_class].get("cooldowns") or {})


def _capacity_cooldown_seconds(failure_count: int) -> int:
    idx = max(0, min(int(failure_count) - 1, len(CAPACITY_COOLDOWN_SECONDS) - 1))
    return CAPACITY_COOLDOWN_SECONDS[idx]


COOLDOWN_BLOCK_REASONS = {
    "selected_model_at_capacity",
    "review_timed_out",
    "review_transport_stalled",
}
MARKED_COOLDOWN_BLOCK_REASONS = {
    "review_interrupted",
    "missing_reviewer_output",
    "reviewer_process_exited",
}


def _cooldown_unavailability_message(*, task_class: str, cooling: dict[str, dict[str, Any]], needed: int) -> str:
    details = ", ".join(
        f"{variant_id} until {format_cooldown_until_for_display(entry.get('until'))}"
        for variant_id, entry in sorted(cooling.items())
    )
    return f"need at least {needed} active non-cooling variants for {task_class}; cooling down: {details}"


def variant_index(roster: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {variant["id"]: variant for variant in roster["variants"]}


def eligible_variants(roster: dict[str, Any], task_class: str) -> list[dict[str, Any]]:
    return [
        variant
        for variant in roster["variants"]
        if variant.get("state", "active") == "active"
        and task_class in variant.get("task_classes", [])
    ]


def task_class_config(roster: dict[str, Any], task_class: str) -> dict[str, Any]:
    config = (roster.get("task_classes") or {}).get(task_class) or {}
    return config if isinstance(config, dict) else {}


def configured_pair_selection_mode(roster: dict[str, Any], task_class: str) -> str:
    task_config = task_class_config(roster, task_class)
    raw_mode = task_config.get("selection_mode", (roster.get("settings") or {}).get("selection_mode", "legacy"))
    mode = str(raw_mode or "legacy").strip()
    if mode == "scramble":
        return "legacy"
    if mode not in PAIR_SELECTION_MODES:
        allowed = ", ".join(PAIR_SELECTION_MODES)
        raise ValueError(f"unknown selection_mode for {task_class}: {mode!r}; expected one of: {allowed}")
    return mode


def summarize_counts(records: list[dict[str, Any]], task_class: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        if record.get("task_class") != task_class:
            continue
        for run in record.get("runs", []):
            variant_id = run["variant_id"]
            counts[variant_id] = counts.get(variant_id, 0) + 1
    return counts


def initial_weight(
    variant: dict[str, Any],
    sample_count: int,
    settings: dict[str, Any],
    *,
    relative_target: float | None = None,
) -> float:
    target = float(bootstrap_target_samples(variant, settings))
    if relative_target is not None:
        target = max(target, float(relative_target))
    if sample_count >= target:
        return 1.0
    boost = float(settings.get("bootstrap_weight_boost", 2.0))
    remaining = target - sample_count
    return 1.0 + (remaining / max(target, 1)) * boost


def bootstrap_target_samples(variant: dict[str, Any], settings: dict[str, Any]) -> int:
    return int(variant.get("bootstrap_target_samples", settings.get("default_bootstrap_target_samples", 8)))


def relative_underuse_target(variants: list[dict[str, Any]], sample_counts: dict[str, int], settings: dict[str, Any]) -> float | None:
    ratio = float(settings.get("relative_underuse_ratio", 0.0) or 0.0)
    if ratio <= 0.0:
        return None
    established_counts = [
        sample_counts.get(variant["id"], 0)
        for variant in variants
        if sample_counts.get(variant["id"], 0) >= bootstrap_target_samples(variant, settings)
    ]
    if not established_counts:
        return None
    return float(statistics.median(established_counts)) * ratio


def effective_underuse_target(
    variant: dict[str, Any],
    variants: list[dict[str, Any]],
    sample_counts: dict[str, int],
    settings: dict[str, Any],
) -> float:
    target = float(bootstrap_target_samples(variant, settings))
    relative_target = relative_underuse_target(variants, sample_counts, settings)
    if relative_target is not None:
        target = max(target, float(relative_target))
    return target


def is_under_sampled(
    variant: dict[str, Any],
    sample_counts: dict[str, int],
    settings: dict[str, Any],
    variants: list[dict[str, Any]],
) -> bool:
    return sample_counts.get(variant["id"], 0) < effective_underuse_target(variant, variants, sample_counts, settings)


def probation_min_samples(settings: dict[str, Any]) -> int:
    return int(settings.get("probation_min_samples", settings.get("promotion_min_samples", 20)))


def probation_max_elo(settings: dict[str, Any]) -> float:
    return float(settings.get("probation_max_elo", 1500.0))


def _acting_champion_variants(
    *,
    roster: dict[str, Any],
    operational_state: dict[str, Any],
    records: list[dict[str, Any]],
    task_class: str,
    available_variants: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    settings = roster.get("settings", {})
    min_samples = int(settings.get("promotion_min_samples", 20))
    probation_ids = set(str(item) for item in (operational_state["task_classes"][task_class].get("probation_variant_ids") or []))
    indexed = variant_index(roster)
    available_ids = {variant["id"] for variant in available_variants}
    leaderboard = aggregate_records(roster=roster, records=records, operational_state=operational_state)["task_classes"][
        task_class
    ]["leaderboard"]
    acting: list[dict[str, Any]] = []
    for row in leaderboard:
        variant_id = str(row.get("variant_id") or "")
        if not variant_id or variant_id not in available_ids or variant_id in probation_ids:
            continue
        if int(row.get("sample_count", 0) or 0) < min_samples:
            continue
        variant = indexed.get(variant_id)
        if variant is not None:
            acting.append(variant)
    return acting


def _seed_with_offset(seed: int | None, offset: int) -> int | None:
    if seed is None:
        return None
    return int(seed) + offset


def pick_weighted_without_replacement(
    variants: list[dict[str, Any]],
    sample_counts: dict[str, int],
    settings: dict[str, Any],
    count: int,
    seed: int | None,
    *,
    relative_target: float | None = None,
) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    available = variants[:]
    chosen: list[dict[str, Any]] = []
    for _ in range(count):
        weights = [
            initial_weight(
                variant,
                sample_counts.get(variant["id"], 0),
                settings,
                relative_target=relative_target,
            )
            for variant in available
        ]
        total = sum(weights)
        roll = rng.uniform(0.0, total)
        upto = 0.0
        pick = 0
        for idx, weight in enumerate(weights):
            upto += weight
            if roll <= upto:
                pick = idx
                break
        chosen.append(available.pop(pick))
    return chosen


def pick_custom_weighted_without_replacement(
    variants: list[dict[str, Any]],
    weights_by_variant_id: dict[str, float],
    count: int,
    seed: int | None,
) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    available = variants[:]
    chosen: list[dict[str, Any]] = []
    for _ in range(count):
        weights = [max(float(weights_by_variant_id.get(variant["id"], 1.0)), 0.01) for variant in available]
        total = sum(weights)
        roll = rng.uniform(0.0, total)
        upto = 0.0
        pick = 0
        for idx, weight in enumerate(weights):
            upto += weight
            if roll <= upto:
                pick = idx
                break
        chosen.append(available.pop(pick))
    return chosen


def compute_cost_usd(variant: dict[str, Any], usage: dict[str, Any]) -> float | None:
    pricing = variant.get("pricing") or {}
    if not pricing:
        return None
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    cached_input_tokens = int(usage.get("cached_input_tokens", 0) or 0)
    if input_tokens + output_tokens <= 0:
        return None
    input_rate = pricing.get("input_per_million_usd")
    output_rate = pricing.get("output_per_million_usd")
    cached_rate = pricing.get("cached_input_per_million_usd")
    if input_rate is None or output_rate is None:
        return None
    total = 0.0
    if cached_rate is None:
        total += (input_tokens / 1_000_000.0) * float(input_rate)
    else:
        uncached_input_tokens = max(0, input_tokens - cached_input_tokens)
        total += (uncached_input_tokens / 1_000_000.0) * float(input_rate)
        total += (cached_input_tokens / 1_000_000.0) * float(cached_rate)
    total += (output_tokens / 1_000_000.0) * float(output_rate)
    return round(total, 6)


def normalize_service_tier(value: Any) -> str | None:
    service_tier = str(value or "").strip().lower()
    if not service_tier:
        return None
    if service_tier not in SUPPORTED_CODEX_SERVICE_TIERS:
        raise ValueError(f"service_tier must be one of: {', '.join(sorted(SUPPORTED_CODEX_SERVICE_TIERS))}")
    return service_tier


def variant_service_tier(variant: dict[str, Any]) -> str | None:
    service_tier = normalize_service_tier(variant.get("service_tier"))
    if not service_tier:
        return None
    if "supported_service_tiers" not in variant:
        return service_tier
    supported = {normalize_service_tier(item) for item in list(variant.get("supported_service_tiers") or [])}
    supported.discard(None)
    if service_tier not in supported:
        raise ValueError(f"variant {variant.get('id') or variant.get('model') or '<unknown>'} does not support service_tier={service_tier}")
    return service_tier


def format_decimal(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{float(value):.1f}"


def format_signed_decimal(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{float(value):+.1f}"


def format_match_model(model: str, outcome: str) -> str:
    if outcome == "win":
        return f"{model} (W)"
    if outcome == "loss":
        return f"{model} (L)"
    if outcome == "tie":
        return f"{model} (T)"
    return model


def format_compact_tokens(value: float | None) -> str:
    if value is None:
        return "-"
    value = float(value)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    if value >= 100_000:
        return f"{value / 1_000:.0f}k"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return f"{value:.0f}"


def format_cost_cents(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100.0:.2f}c"


def total_usage_tokens(usage: dict[str, Any]) -> int:
    return int(usage.get("input_tokens", 0) or 0) + int(usage.get("output_tokens", 0) or 0)


def rounds_dir(state_dir: Path) -> Path:
    return state_dir / ROUNDS_DIRNAME


def round_path(state_dir: Path, round_id: str) -> Path:
    return rounds_dir(state_dir) / f"{round_id}.json"


def normalize_review_cwd_value(review_cwd: Path | str | None) -> str | None:
    if review_cwd is None:
        return None
    try:
        return normalize_cwd(str(review_cwd))
    except Exception:
        return None


def normalize_record_review_cwd_value(payload: dict[str, Any]) -> str | None:
    return normalize_review_cwd_value(payload.get("review_cwd_normalized")) or normalize_review_cwd_value(payload.get("review_cwd"))


def resolve_caller_id(explicit_caller_id: str | None = None) -> tuple[str | None, str | None]:
    explicit = (explicit_caller_id or "").strip()
    if explicit:
        return explicit, "arg"
    subagent_id = (os.environ.get("PWF_SUBAGENT_ID") or "").strip()
    thread_id = (os.environ.get("CODEX_THREAD_ID") or "").strip()
    if subagent_id and thread_id:
        return f"{thread_id}:{subagent_id}", "codex_thread_id+pwf_subagent_id"
    for env_name, source in CALLER_ID_ENV_KEYS:
        value = (os.environ.get(env_name) or "").strip()
        if value:
            return value, source
    return None, None


def _round_has_recorded_grade(payload: dict[str, Any]) -> bool:
    return bool(str(payload.get("graded_at") or "").strip())


def round_needs_caller_grade(payload: dict[str, Any]) -> bool:
    if str(payload.get("status") or "") != "completed":
        return False
    if _round_has_recorded_grade(payload):
        return False
    if payload_has_blocked_runs(payload):
        return False
    return True


def iter_round_payloads(state_dir: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    directory = rounds_dir(state_dir)
    if not directory.exists():
        return entries
    for path in sorted(directory.glob("*.json")):
        try:
            payload = read_json(path)
        except Exception:
            continue
        payload.setdefault("_round_file_path", str(path))
        entries.append(payload)
    return entries


def ungraded_round_exposure_records(state_dir: Path) -> list[dict[str, Any]]:
    cleanup_stale_ungraded_rounds(state_dir)
    records: list[dict[str, Any]] = []
    for payload in iter_round_payloads(state_dir):
        if str(payload.get("status") or "") not in {"sampled", "running", "completed"}:
            continue
        if _round_has_recorded_grade(payload):
            continue
        task_class = str(payload.get("task_class") or "")
        runs = [
            {"variant_id": str(run.get("variant_id") or "")}
            for run in list(payload.get("runs") or [])
            if str(run.get("variant_id") or "")
        ]
        if task_class and runs:
            records.append({"task_class": task_class, "runs": runs})
    return records


def round_is_stale_ungraded(
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
    stale_seconds: int = STALE_REVIEW_STATE_TTL_SECONDS,
) -> bool:
    if str(payload.get("status") or "") not in {"sampled", "running", "completed"}:
        return False
    if _round_has_recorded_grade(payload):
        return False
    if round_has_live_reviewer_process(payload):
        return False
    reference_time = _payload_reference_time(payload)
    if reference_time is None:
        return False
    return ((now or utc_now()) - reference_time).total_seconds() >= stale_seconds


def cleanup_stale_ungraded_rounds(
    state_dir: Path,
    *,
    stale_seconds: int = STALE_REVIEW_STATE_TTL_SECONDS,
) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    now = utc_now()
    dismissed_at = now.isoformat().replace("+00:00", "Z")
    reason = f"auto_stale_ungraded_round_{max(1, stale_seconds // 3600)}h"
    for payload in iter_round_payloads(state_dir):
        if not round_is_stale_ungraded(payload, now=now, stale_seconds=stale_seconds):
            continue
        previous_status = str(payload.get("status") or "unknown")
        payload["status"] = "dismissed"
        payload["dismissed_at"] = dismissed_at
        payload["dismissed_reason"] = reason
        payload["dismissed_previous_status"] = previous_status
        payload.pop("_round_file_path", None)
        write_round(state_dir, payload)
        cleaned.append(
            {
                "round_id": str(payload.get("round_id") or ""),
                "previous_status": previous_status,
                "reason": reason,
            }
        )
    return cleaned


def find_pending_rounds_for_caller(*, state_dir: Path, caller_id: str | None, review_cwd: Path | str | None) -> list[dict[str, Any]]:
    cleanup_stale_ungraded_rounds(state_dir)
    normalized_review_cwd = normalize_review_cwd_value(review_cwd)
    caller = (caller_id or "").strip()
    if not caller or not normalized_review_cwd:
        return []
    pending: list[dict[str, Any]] = []
    for payload in iter_round_payloads(state_dir):
        if str(payload.get("caller_id") or "").strip() != caller:
            continue
        if normalize_record_review_cwd_value(payload) != normalized_review_cwd:
            continue
        if not round_needs_caller_grade(payload):
            continue
        pending.append(payload)
    pending.sort(key=lambda item: str(item.get("review_completed_at") or item.get("sampled_at") or item.get("round_id") or ""))
    return pending


def find_blocking_rounds_for_caller(*, state_dir: Path, caller_id: str | None, review_cwd: Path | str | None) -> list[dict[str, Any]]:
    cleanup_stale_ungraded_rounds(state_dir)
    normalized_review_cwd = normalize_review_cwd_value(review_cwd)
    caller = (caller_id or "").strip()
    if not caller or not normalized_review_cwd:
        return []
    blocking: list[dict[str, Any]] = []
    for payload in iter_round_payloads(state_dir):
        if str(payload.get("caller_id") or "").strip() != caller:
            continue
        if normalize_record_review_cwd_value(payload) != normalized_review_cwd:
            continue
        status = str(payload.get("status") or "")
        if round_needs_caller_grade(payload) or status in {"sampled", "running"} or round_has_live_reviewer_process(payload):
            blocking.append(payload)
    blocking.sort(key=lambda item: str(item.get("review_completed_at") or item.get("sampled_at") or item.get("round_id") or ""))
    return blocking


def repo_token(review_cwd: Path | None) -> str | None:
    if review_cwd is None:
        return None
    normalized = str(review_cwd.resolve())
    name = re.sub(r"[^a-z0-9]+", "-", review_cwd.name.lower()).strip("-") or "repo"
    digest = blake2s(normalized.encode("utf-8"), digest_size=3).hexdigest()
    return f"{name}-{digest}"


def make_round_id(task_class: str, *, review_cwd: Path | None = None) -> str:
    token = repo_token(review_cwd)
    prefix = f"{task_class}-{token}" if token else task_class
    return f"{prefix}-{utc_now().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


def public_reviewer_label(slot: str) -> str:
    if slot in PUBLIC_REVIEWER_LABELS:
        return slot
    if slot.startswith("reviewer_"):
        try:
            idx = int(slot.split("_", 1)[1]) - 1
        except ValueError:
            return slot
        if 0 <= idx < len(PUBLIC_REVIEWER_LABELS):
            return PUBLIC_REVIEWER_LABELS[idx]
    return slot


def _elapsed_seconds_for_runs(runs: list[dict[str, Any]]) -> int:
    elapsed = 0
    for run in runs:
        started_at = run.get("started_at")
        if not started_at:
            continue
        value = int((utc_now() - datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))).total_seconds())
        elapsed = max(elapsed, value)
    return elapsed


def _running_status_line(runs: list[dict[str, Any]]) -> str:
    labels = [public_reviewer_label(str(run["slot"])) for run in runs]
    elapsed = _elapsed_seconds_for_runs(runs)
    return f"Running: {elapsed}s {', '.join(labels)}"


def _heartbeat_status_line(runs: list[dict[str, Any]]) -> str:
    labels = [public_reviewer_label(str(run["slot"])) for run in runs]
    minutes = max(1, _elapsed_seconds_for_runs(runs) // 60)
    return f"OK {minutes}m: {','.join(labels)}"


def _progress_status_line(runs: list[dict[str, Any]]) -> str:
    if progress_output_isatty():
        return _running_status_line(runs)
    return _heartbeat_status_line(runs)


def _run_started_at_datetime(run: dict[str, Any]) -> datetime | None:
    started_at = run.get("started_at")
    if not started_at:
        return None
    try:
        return datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
    except ValueError:
        return None


def _path_stat(path_text: str) -> os.stat_result | None:
    path = Path(path_text)
    try:
        return path.stat()
    except OSError:
        return None


def _artifact_activity_epoch(run: dict[str, Any]) -> float | None:
    times: list[float] = []
    for key in ("stdout_path", "stderr_path"):
        stat = _path_stat(str(run.get(key) or ""))
        if stat is not None:
            times.append(float(stat.st_mtime))
    return max(times) if times else None


def _stdout_has_content(run: dict[str, Any]) -> bool:
    stat = _path_stat(str(run.get("stdout_path") or ""))
    return bool(stat is not None and int(stat.st_size) > 0)


def _stderr_text_for_run(run: dict[str, Any]) -> str:
    path_text = str(run.get("stderr_path") or "").strip()
    if not path_text:
        return ""
    path = Path(path_text)
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _transport_stall_signal(stderr_text: str) -> str | None:
    lowered = stderr_text.lower()
    if "falling back to http" in lowered:
        return "http_fallback_no_output"
    if "reconnecting... 5/5" in lowered or "retrying sampling request (5/5" in lowered:
        return "reconnect_exhausted_no_output"
    return None


def _transport_stalled(run: dict[str, Any], *, now_epoch: float | None = None) -> str | None:
    if _stdout_has_content(run):
        return None
    stderr_text = _stderr_text_for_run(run)
    signal = _transport_stall_signal(stderr_text)
    if signal is None:
        return None
    last_activity = _artifact_activity_epoch(run)
    if last_activity is None:
        return None
    now = time.time() if now_epoch is None else now_epoch
    if now - last_activity < TRANSPORT_STALL_GRACE_SECONDS:
        return None
    return signal


def _transport_hung_after_output(run: dict[str, Any], *, now_epoch: float | None = None) -> str | None:
    if not _stdout_has_content(run):
        return None
    last_activity = _artifact_activity_epoch(run)
    if last_activity is None:
        return None
    now = time.time() if now_epoch is None else now_epoch
    if now - last_activity < TRANSPORT_STALL_GRACE_SECONDS:
        return None
    return "output_captured_process_still_running"


def _terminate_process_tree(pid: int | None) -> None:
    if not pid:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    try:
        os.kill(int(pid), 15)
    except OSError:
        pass


def _transport_event_lines(stderr_text: str, *, start_offset: int) -> tuple[list[str], int]:
    offset = max(0, int(start_offset or 0))
    chunk = stderr_text[offset:]
    lines = [
        line.strip()
        for line in chunk.splitlines()
        if line.strip() and any(pattern in line for pattern in TRANSPORT_RECONNECT_PATTERNS)
    ]
    return lines, len(stderr_text)


def _print_transport_events(active_runs: list[dict[str, Any]]) -> bool:
    printed = False
    for run in active_runs:
        slot = str(run.get("slot") or "")
        if not slot:
            continue
        stderr_text = _stderr_text_for_run(run)
        start_offset = int(run.get("stderr_progress_offset", 0) or 0)
        lines, next_offset = _transport_event_lines(stderr_text, start_offset=start_offset)
        run["stderr_progress_offset"] = next_offset
        label = public_reviewer_label(slot)
        for line in lines:
            print(f"[review-suite] {label} transport: {line}", file=sys.stderr, flush=True)
            printed = True
    return printed


def _live_review_thread(
    *,
    run: dict[str, Any],
    variant: dict[str, Any],
    sqlite_path: Path,
    review_cwd: Path,
) -> dict[str, Any] | None:
    stderr_path_text = str(run.get("stderr_path") or "").strip()
    stderr_path = Path(stderr_path_text) if stderr_path_text else None
    stderr_text = (
        stderr_path.read_text(encoding="utf-8", errors="replace")
        if stderr_path is not None and stderr_path.is_file()
        else ""
    )
    session_id = extract_session_id(stderr_text)
    review_cwd_text = str(review_cwd)
    started_at = _started_at_epoch_seconds(run.get("started_at"))
    created_after = max(0, started_at - 5) if started_at is not None else int(time.time()) - 5
    title = str(run.get("title") or "")
    model = str(variant["model"])
    reasoning_effort = str(variant["reasoning_effort"])

    def is_direct_review_thread(candidate: dict[str, Any], *, require_session_match: bool) -> bool:
        if str(candidate.get("source") or "") == REVIEW_SUBAGENT_SOURCE:
            return True
        if require_session_match and str(candidate.get("id") or "") != str(session_id or ""):
            return False
        if not require_session_match:
            return False
        try:
            candidate_created_at = int(candidate.get("created_at"))
        except (TypeError, ValueError):
            candidate_created_at = None
        if candidate_created_at is None or candidate_created_at < created_after:
            return False
        return (
            title
            and str(candidate.get("title") or "") == title
            and str(candidate.get("cwd") or "") == review_cwd_text
            and str(candidate.get("model") or "").lower() == model.lower()
            and str(candidate.get("reasoning_effort") or "").lower() == reasoning_effort.lower()
        )

    if session_id:
        candidate = find_thread_by_id(sqlite_path=sqlite_path, thread_id=session_id)
        if candidate:
            if is_direct_review_thread(candidate, require_session_match=True):
                return candidate
            child = find_review_child_thread(
                sqlite_path=sqlite_path,
                cwd=str(candidate.get("cwd") or review_cwd_text),
                title=str(candidate.get("title") or ""),
                model=model,
                reasoning_effort=reasoning_effort,
                created_at=candidate.get("created_at"),
                updated_at=candidate.get("updated_at"),
            )
            if child:
                return child
            return None
    child = find_review_child_thread(
        sqlite_path=sqlite_path,
        cwd=review_cwd_text,
        title=title,
        model=model,
        reasoning_effort=reasoning_effort,
        created_at=started_at,
    )
    if child:
        return child
    candidate = find_thread_by_title(
        sqlite_path=sqlite_path,
        title=title,
        cwd=review_cwd_text,
        created_after=created_after,
    )
    if not candidate:
        return None
    if is_direct_review_thread(candidate, require_session_match=False):
        return candidate
    child = find_review_child_thread(
        sqlite_path=sqlite_path,
        cwd=str(candidate.get("cwd") or review_cwd_text),
        title=str(candidate.get("title") or ""),
        model=model,
        reasoning_effort=reasoning_effort,
        created_at=candidate.get("created_at"),
        updated_at=candidate.get("updated_at"),
    )
    return child


def _print_stall_warnings(
    *,
    active_runs: list[dict[str, Any]],
    indexed: dict[str, dict[str, Any]],
    sqlite_path: Path,
    review_cwd: Path,
    warned_slots: set[str],
) -> None:
    now = utc_now()
    for run in active_runs:
        slot = str(run.get("slot") or "")
        if not slot or slot in warned_slots:
            continue
        variant = dict(run.get("variant") or indexed.get(str(run.get("variant_id") or ""), {}))
        if not variant:
            continue
        thread = _live_review_thread(
            run=run,
            variant=variant,
            sqlite_path=sqlite_path,
            review_cwd=review_cwd,
        )
        rollout_path_text = str((thread or {}).get("rollout_path") or "")
        if not rollout_path_text:
            continue
        rollout_path = Path(rollout_path_text)
        if not rollout_path.is_file():
            continue
        activity = rollout_activity_summary(rollout_path)
        last_meaningful_at = activity.get("last_meaningful_at") or _run_started_at_datetime(run)
        if last_meaningful_at is None:
            continue
        idle_seconds = int((now - last_meaningful_at).total_seconds())
        if idle_seconds < REVIEW_STALL_WARNING_SECONDS:
            continue
        label = public_reviewer_label(slot)
        print(
            f"[review-suite] possible stall: {label} idle {idle_seconds // 60}m; wrapper will keep waiting.",
            file=sys.stderr,
            flush=True,
        )
        warned_slots.add(slot)


def reviewer_completion_status(run: dict[str, Any]) -> str:
    block = str(run.get("grade_block_reason") or "").strip()
    if block:
        return block
    status = str(run.get("review_status") or "").strip()
    return status or "unknown"


def output_isatty() -> bool:
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def progress_output_isatty() -> bool:
    return bool(getattr(sys.stderr, "isatty", lambda: False)())


def reviewer_output_heading(run: dict[str, Any]) -> str:
    label = public_reviewer_label(str(run.get("slot") or "reviewer"))
    status = str(run.get("review_status") or "unknown")
    if status == "completed":
        return f"{label}:"
    return f"{label} [{status}]:"


def _print_live_completed_run(run: dict[str, Any]) -> None:
    write_text(f"{reviewer_output_heading(run)} {reviewer_completion_status(run)}", stream=sys.stderr)


def final_display_body(run: dict[str, Any]) -> str:
    reviewer_output = str(run.get("reviewer_output") or "").strip()
    if reviewer_output:
        return reviewer_output
    status_summary = str(run.get("status_summary") or "").strip()
    if status_summary:
        return status_summary
    status = str(run.get("review_status") or "").strip()
    return f"({status or 'no output'})"


def print_reviewer_output_section(runs: list[dict[str, Any]]) -> bool:
    if not runs:
        return False
    write_text("Output:")
    for index, raw_run in enumerate(runs):
        run = _finalized_run_summary(raw_run)
        if index:
            write_text("")
        write_text(reviewer_output_heading(run))
        write_text(final_display_body(run))
    return True


def review_label(task_class: str) -> str:
    public = public_task_name(task_class)
    if public != task_class:
        return public
    if task_class == "phase_review":
        return "phase"
    if task_class == "pr_review":
        return "pr"
    return str(task_class)


def public_task_name(task_class: str) -> str:
    if task_class == "phase_review":
        return "review_t1"
    if task_class == "phase_gate":
        return "review_t2"
    if task_class == "pr_review":
        return "review_t3"
    if task_class == "pr_gate":
        return "review_t4"
    return str(task_class)


def repo_name_from_round_payload(payload: dict[str, Any]) -> str:
    review_cwd = str(payload.get("review_cwd") or payload.get("review_cwd_normalized") or "").strip()
    if not review_cwd:
        return "-"
    trimmed = review_cwd.rstrip("\\/")
    if not trimmed:
        return "-"
    return re.split(r"[\\/]+", trimmed)[-1] or "-"


def round_repo_name(state_dir: Path, round_id: str, cache: dict[str, str]) -> str:
    cached = cache.get(round_id)
    if cached is not None:
        return cached
    name = "-"
    try:
        payload = read_json(round_path(state_dir, round_id))
        name = repo_name_from_round_payload(payload)
    except Exception:
        pass
    cache[round_id] = name
    return name


def recent_match_history(state_dir: Path, *, k_factor: float, limit: int = 10) -> list[dict[str, Any]]:
    ratings_by_task: dict[str, dict[str, float]] = {task_class: {} for task_class in TASK_CLASSES}
    repo_cache: dict[str, str] = {}
    history: list[dict[str, Any]] = []
    for record in read_jsonl(state_dir / RUN_LOG_FILENAME):
        task_class = str(record.get("task_class") or "")
        record_runs = list(record.get("runs") or [])
        if len(record_runs) != 2:
            continue
        alpha_run, beta_run = record_runs[0], record_runs[1]
        alpha_model = str(alpha_run.get("variant_id") or "")
        beta_model = str(beta_run.get("variant_id") or "")
        if not alpha_model or not beta_model:
            continue
        winner = str((record.get("pairwise_outcome") or {}).get("winner") or "")
        if winner == "tie":
            result = "tie"
        elif winner == alpha_model:
            result = "a"
        elif winner == beta_model:
            result = "b"
        else:
            continue
        ratings = ratings_by_task.setdefault(task_class, {})
        alpha_before = float(ratings.get(alpha_model, 1500.0))
        beta_before = float(ratings.get(beta_model, 1500.0))
        alpha_after, beta_after = update_elo(alpha_before, beta_before, result, k_factor)
        ratings[alpha_model] = alpha_after
        ratings[beta_model] = beta_after
        history.append(
            {
                "recorded_at": str(record.get("recorded_at") or ""),
                "review": review_label(task_class),
                "repo": round_repo_name(state_dir, str(record.get("round_id") or ""), repo_cache),
                "model_alpha": alpha_model,
                "outcome_alpha": "win" if result == "a" else "loss" if result == "b" else "tie",
                "elo_alpha": alpha_before,
                "delta_alpha": alpha_after - alpha_before,
                "delta_beta": beta_after - beta_before,
                "elo_beta": beta_before,
                "model_beta": beta_model,
                "outcome_beta": "win" if result == "b" else "loss" if result == "a" else "tie",
            }
        )
    history.sort(key=lambda row: row["recorded_at"], reverse=True)
    return history[:limit]


def public_round_payload(payload: dict[str, Any], *, task_name: str | None = None) -> dict[str, Any]:
    return {
        "round_id": payload["round_id"],
        "task": task_name or public_task_name(str(payload["task_class"])),
        "status": payload.get("status"),
        "sampled_at": payload.get("sampled_at"),
        "reviewers": [public_reviewer_label(run["slot"]) for run in payload.get("runs", [])],
    }


def usable_output_slots(payload: dict[str, Any]) -> set[str]:
    visible: set[str] = set()
    for raw_run in payload.get("runs", []):
        if not isinstance(raw_run, dict):
            continue
        slot = str(raw_run.get("slot") or "")
        if not slot:
            continue
        classification = _classification_for_run(raw_run)
        if classification["grade_blocked"]:
            continue
        if classification["review_status"] != "completed":
            continue
        if not str(raw_run.get("reviewer_output") or "").strip():
            continue
        visible.add(slot)
    return visible


def payload_has_blocked_runs(payload: dict[str, Any]) -> bool:
    for raw_run in payload.get("runs", []):
        if not isinstance(raw_run, dict):
            continue
        if _classification_for_run(raw_run)["grade_blocked"]:
            return True
    return False


def _finalized_run_summary(run: dict[str, Any]) -> dict[str, Any]:
    classification = _classification_for_run(run)
    normalized = deepcopy(run)
    normalized["review_status"] = classification["review_status"]
    normalized["status_summary"] = classification["status_summary"]
    normalized["grade_blocked"] = classification["grade_blocked"]
    normalized["grade_block_reason"] = classification["grade_block_reason"]
    return normalized


def _run_is_finalized(run: dict[str, Any]) -> bool:
    if run.get("review_status"):
        return True
    if run.get("returncode") is not None:
        return True
    if run.get("reviewer_output"):
        return True
    return False


def public_round_result(payload: dict[str, Any], *, output_slots: set[str] | None = None) -> dict[str, Any]:
    run_summaries = []
    for run in payload.get("runs", []):
        classification = _classification_for_run(run)
        row = {
            "slot": public_reviewer_label(run["slot"]),
            "status": classification["review_status"],
            "summary": classification["status_summary"],
            "ref": run.get("reviewer_output_ref"),
            "blocked": classification["grade_blocked"],
            "block": classification["grade_block_reason"],
        }
        run_summaries.append(row)
    result = {
        "round_id": payload["round_id"],
        "task": public_task_name(str(payload.get("task_class") or "")),
        "status": payload["status"],
        "blocked": any(bool(run.get("blocked")) for run in run_summaries),
        "runs": run_summaries,
    }
    if payload.get("cooldown_updates"):
        result["cooldowns"] = [
            {
                "variant": str(update.get("variant_id") or ""),
                "until": format_cooldown_until_for_display(update.get("cooldown_until")),
                "reason": update.get("reason"),
                "failures": update.get("failure_count"),
            }
            for update in list(payload.get("cooldown_updates") or [])
        ]
    return result


def _report_settings_snapshot(settings: dict[str, Any]) -> dict[str, Any]:
    return {
        "elo_k_factor": float(settings.get("elo_k_factor", 24.0)),
        "promotion_min_samples": int(settings.get("promotion_min_samples", 20)),
        "promotion_min_elo": float(settings.get("promotion_min_elo", 1550.0)),
        "promotion_champion_group_window": float(
            settings.get("promotion_champion_group_window", settings.get("promotion_min_elo_lead", 25.0))
        ),
    }


def _probation_variant_ids(
    *, leaderboard: list[dict[str, Any]], champion_ids: set[str], settings: dict[str, Any]
) -> list[str]:
    min_samples = probation_min_samples(settings)
    max_elo = probation_max_elo(settings)
    probation: list[str] = []
    for row in leaderboard:
        variant_id = str(row["variant_id"])
        if variant_id in champion_ids:
            continue
        if int(row.get("sample_count", 0) or 0) < min_samples:
            continue
        if float(row.get("elo", 0.0) or 0.0) >= max_elo:
            continue
        if int(row.get("loss_count", 0) or 0) <= int(row.get("win_count", 0) or 0):
            continue
        probation.append(variant_id)
    return probation


def select_pair(
    *,
    roster: dict[str, Any],
    operational_state: dict[str, Any],
    records: list[dict[str, Any]],
    task_class: str,
    review_cwd: Path | None,
    seed: int | None,
    caller_id: str | None = None,
    caller_id_source: str | None = None,
    excluded_variant_ids: set[str] | None = None,
) -> dict[str, Any]:
    settings = roster.get("settings", {})
    excluded_variant_ids = excluded_variant_ids or set()
    indexed = variant_index(roster)
    all_variants = [
        variant
        for variant in eligible_variants(roster, task_class)
        if variant["id"] not in excluded_variant_ids
    ]
    cooling = _active_cooldowns(operational_state, task_class)
    variants = [variant for variant in all_variants if variant["id"] not in cooling]
    if len(variants) < 2:
        if len(all_variants) >= 2 and cooling:
            raise ValueError(_cooldown_unavailability_message(task_class=task_class, cooling=cooling, needed=2))
        raise ValueError(f"need at least two active variants for {task_class}")
    counts = summarize_counts(records, task_class)
    state = operational_state["task_classes"][task_class]
    available_ids = {variant["id"] for variant in variants}
    probation_ids: list[str] = [
        variant_id
        for variant_id in list(state.get("probation_variant_ids") or [])
        if variant_id in available_ids
    ]
    selection_mode = configured_pair_selection_mode(roster, task_class)
    if selection_mode == "legacy":
        selected, selection_pairing = _select_scramble_pair(
            variants=variants,
            reference_variants=all_variants,
            probation_ids=probation_ids,
            counts=counts,
            settings=settings,
            seed=seed,
        )
    elif selection_mode == "true_scramble":
        selected, selection_pairing = _select_true_scramble_pair(variants=variants, seed=seed)
    else:
        leaderboard = aggregate_records(
            roster=roster,
            records=records,
            operational_state=operational_state,
        )["task_classes"][task_class]["leaderboard"]
        selected, selection_pairing = _select_slight_bias_pair(
            variants=variants,
            leaderboard=leaderboard,
            settings=settings,
            seed=seed,
        )

    return {
        "round_id": make_round_id(task_class, review_cwd=review_cwd),
        "task_class": task_class,
        "selection_mode": selection_mode,
        "selection_pairing": selection_pairing,
        "selection_champion_variant_ids": [],
        "selection_anchor_kind": None,
        "selection_fallback_reason": None,
        "sampled_at": utc_now_iso(),
        "caller_id": caller_id,
        "caller_id_source": caller_id_source,
        "review_cwd_normalized": normalize_review_cwd_value(review_cwd),
        "excluded_variant_ids": sorted(excluded_variant_ids),
        "status": "sampled",
        "runs": [
            {
                "slot": PUBLIC_REVIEWER_LABELS[idx],
                "variant_id": variant["id"],
                "model": variant["model"],
                "reasoning_effort": variant["reasoning_effort"],
            }
            for idx, variant in enumerate(selected)
        ],
    }


def _select_scramble_pair(
    *,
    variants: list[dict[str, Any]],
    reference_variants: list[dict[str, Any]],
    probation_ids: list[str],
    counts: dict[str, int],
    settings: dict[str, Any],
    seed: int | None,
) -> tuple[list[dict[str, Any]], str]:
    underuse_target = relative_underuse_target(reference_variants, counts, settings)
    probation_id_set = set(probation_ids)
    under_sampled = [
        variant
        for variant in variants
        if variant["id"] not in probation_id_set
        and is_under_sampled(variant, counts, settings, reference_variants)
    ]
    if under_sampled:
        lowest_exposure = min(counts.get(variant["id"], 0) for variant in under_sampled)
        under_sampled = [variant for variant in under_sampled if counts.get(variant["id"], 0) == lowest_exposure]
    if not under_sampled:
        preferred_variants = [variant for variant in variants if variant["id"] not in probation_id_set]
        probation_variants = [variant for variant in variants if variant["id"] in probation_id_set]
        if preferred_variants:
            anchor = pick_weighted_without_replacement(
                preferred_variants,
                counts,
                settings,
                1,
                _seed_with_offset(seed, 101),
                relative_target=underuse_target,
            )[0]
            remaining_preferred = [
                variant for variant in preferred_variants if variant["id"] != anchor["id"]
            ]
            probation_probe_ratio = float(settings.get("scramble_probation_probe_ratio", 0.15))
            want_probation_probe = bool(probation_variants) and (
                not remaining_preferred
                or (
                    probation_probe_ratio > 0.0
                    and random.Random(_seed_with_offset(seed, 100)).random() < probation_probe_ratio
                )
            )
            if want_probation_probe:
                probe = pick_weighted_without_replacement(
                    probation_variants,
                    counts,
                    settings,
                    1,
                    _seed_with_offset(seed, 102),
                    relative_target=underuse_target,
                )[0]
                return [anchor, probe], "scramble_weighted_vs_probation"
            if remaining_preferred:
                second = pick_weighted_without_replacement(
                    remaining_preferred,
                    counts,
                    settings,
                    1,
                    _seed_with_offset(seed, 102),
                    relative_target=underuse_target,
                )[0]
                return [anchor, second], "scramble_weighted"
        highest_sample_count = max(counts.get(variant["id"], 0) for variant in variants)
        anchor_pool = [
            variant for variant in variants if counts.get(variant["id"], 0) == highest_sample_count
        ]
        anchor = pick_weighted_without_replacement(
            anchor_pool,
            counts,
            settings,
            1,
            _seed_with_offset(seed, 103),
            relative_target=underuse_target,
        )[0]
        remaining_variants = [variant for variant in variants if variant["id"] != anchor["id"]]
        second = pick_weighted_without_replacement(
            remaining_variants,
            counts,
            settings,
            1,
            _seed_with_offset(seed, 104),
            relative_target=underuse_target,
        )[0]
        return [anchor, second], "scramble_weighted_best_available"
    exploration = pick_weighted_without_replacement(
        under_sampled,
        counts,
        settings,
        1,
        _seed_with_offset(seed, 1),
        relative_target=underuse_target,
    )[0]
    rest_pool = [
        variant
        for variant in variants
        if variant["id"] != exploration["id"]
        and not is_under_sampled(variant, counts, settings, reference_variants)
        and variant["id"] not in probation_id_set
    ]
    pairing = "scramble_exploration_vs_rest"
    if not rest_pool:
        degraded_pool = [variant for variant in variants if variant["id"] != exploration["id"] and variant["id"] not in probation_id_set]
        if not degraded_pool:
            degraded_pool = [variant for variant in variants if variant["id"] != exploration["id"]]
        if not degraded_pool:
            raise ValueError("scramble sampling requires at least two eligible variants")
        highest_sample_count = max(counts.get(variant["id"], 0) for variant in degraded_pool)
        rest_pool = [
            variant for variant in degraded_pool if counts.get(variant["id"], 0) == highest_sample_count
        ]
        pairing = "scramble_exploration_vs_best_available"
    stability = pick_weighted_without_replacement(
        rest_pool,
        counts,
        settings,
        1,
        _seed_with_offset(seed, 2),
        relative_target=underuse_target,
    )[0]
    return [exploration, stability], pairing


def _select_true_scramble_pair(
    *,
    variants: list[dict[str, Any]],
    seed: int | None,
) -> tuple[list[dict[str, Any]], str]:
    return random.Random(seed).sample(variants, 2), "true_scramble_random"


def _select_slight_bias_pair(
    *,
    variants: list[dict[str, Any]],
    leaderboard: list[dict[str, Any]],
    settings: dict[str, Any],
    seed: int | None,
) -> tuple[list[dict[str, Any]], str]:
    ratings = {str(row.get("variant_id")): float(row.get("elo", 1500.0) or 1500.0) for row in leaderboard}
    active_ratings = [ratings.get(variant["id"], 1500.0) for variant in variants]
    center = statistics.mean(active_ratings) if active_ratings else 1500.0
    strength = max(float(settings.get("slight_bias_elo_weight", 0.15) or 0.0), 0.0)
    scale = max(float(settings.get("slight_bias_elo_scale", 400.0) or 400.0), 1.0)
    weights = {}
    for variant in variants:
        normalized_delta = max(min((ratings.get(variant["id"], 1500.0) - center) / scale, 1.0), -1.0)
        weights[variant["id"]] = 1.0 + (strength * normalized_delta)
    selected = pick_custom_weighted_without_replacement(variants, weights, 2, seed)
    return selected, "slight_bias_elo_weighted"


def _pick_weighted_pool(pool_weights: list[tuple[str, float]], seed: int | None) -> str:
    rng = random.Random(seed)
    total = sum(weight for _, weight in pool_weights)
    threshold = rng.random() * total
    running = 0.0
    for name, weight in pool_weights:
        running += weight
        if threshold <= running:
            return name
    return pool_weights[-1][0]


def _select_champion_pair(
    *,
    variants: list[dict[str, Any]],
    reference_variants: list[dict[str, Any]],
    indexed: dict[str, dict[str, Any]],
    champion_ids: list[str],
    probation_ids: list[str],
    counts: dict[str, int],
    settings: dict[str, Any],
    seed: int | None,
    anchor_override: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    champion_pool = [indexed[variant_id] for variant_id in champion_ids if variant_id in indexed]
    probation_id_set = set(probation_ids)
    underuse_target = relative_underuse_target(reference_variants, counts, settings)
    if anchor_override is not None:
        anchor = anchor_override
    else:
        anchor = pick_weighted_without_replacement(
            champion_pool,
            counts,
            settings,
            1,
            _seed_with_offset(seed, 10),
            relative_target=underuse_target,
        )[0]
    new_challenger_pool = [
        variant
        for variant in variants
        if variant["id"] != anchor["id"]
        and variant["id"] not in champion_ids
        and variant["id"] not in probation_id_set
        and is_under_sampled(variant, counts, settings, reference_variants)
    ]
    contender_pool = [
        variant
        for variant in variants
        if variant["id"] != anchor["id"]
        and variant["id"] not in champion_ids
        and variant["id"] not in probation_id_set
        and variant["id"] not in {candidate["id"] for candidate in new_challenger_pool}
    ]
    probation_pool = [
        variant
        for variant in variants
        if variant["id"] != anchor["id"]
        and variant["id"] not in champion_ids
        and variant["id"] in probation_id_set
    ]
    champion_opponent_pool = (
        [variant for variant in champion_pool if variant["id"] != anchor["id"]]
        if anchor_override is None
        else []
    )
    pools: list[tuple[str, list[dict[str, Any]], float]] = []
    if champion_opponent_pool:
        pools.append(("champion_vs_champion", champion_opponent_pool, 0.50))
    if new_challenger_pool:
        pools.append(("champion_vs_new_challenger", new_challenger_pool, 0.20))
    if contender_pool:
        pools.append(("champion_vs_contender", contender_pool, 0.20))
    if probation_pool:
        pools.append(("champion_vs_probation", probation_pool, 0.10))
    if not pools:
        fallback = [variant for variant in variants if variant["id"] != anchor["id"]]
        opponent = pick_weighted_without_replacement(
            fallback,
            counts,
            settings,
            1,
            _seed_with_offset(seed, 11),
            relative_target=underuse_target,
        )[0]
        return [anchor, opponent], "champion_vs_fallback"
    chosen_name = _pick_weighted_pool([(name, weight) for name, _, weight in pools], _seed_with_offset(seed, 11))
    chosen_pool = next(pool for name, pool, _ in pools if name == chosen_name)
    opponent = pick_weighted_without_replacement(
        chosen_pool,
        counts,
        settings,
        1,
        _seed_with_offset(seed, 12),
        relative_target=underuse_target,
    )[0]
    return [anchor, opponent], chosen_name


def _reroll_candidate_variants(
    *,
    roster: dict[str, Any],
    operational_state: dict[str, Any],
    records: list[dict[str, Any]],
    round_payload: dict[str, Any],
    slot: str,
    excluded_variant_ids: set[str],
) -> list[dict[str, Any]]:
    settings = roster.get("settings", {})
    task_class = str(round_payload["task_class"])
    all_variants = [
        variant
        for variant in eligible_variants(roster, task_class)
        if variant["id"] not in excluded_variant_ids
    ]
    cooling = _active_cooldowns(operational_state, task_class)
    variants = [variant for variant in all_variants if variant["id"] not in cooling]
    counts = summarize_counts(records, task_class)
    probation_ids = set(str(item) for item in (operational_state["task_classes"][task_class].get("probation_variant_ids") or []))
    if round_payload.get("selection_mode") == "champion":
        champion_ids = set(str(item) for item in (round_payload.get("selection_champion_variant_ids") or []))
        pairing = str(round_payload.get("selection_pairing") or "")
        if slot == "alpha" or pairing == "champion_vs_champion":
            pool = [variant for variant in variants if variant["id"] in champion_ids]
            if pool:
                return pool
            acting_champions = _acting_champion_variants(
                roster=roster,
                operational_state=operational_state,
                records=records,
                task_class=task_class,
                available_variants=variants,
            )
            if acting_champions:
                return acting_champions
            raise ValueError(
                f"no compatible champion or acting-champion reroll variants remain for {task_class} ({pairing}, {slot})"
            )
        if pairing == "champion_vs_new_challenger":
            pool = [
                variant
                for variant in variants
                if variant["id"] not in champion_ids
                and variant["id"] not in probation_ids
                and is_under_sampled(variant, counts, settings, all_variants)
            ]
            if pool:
                return pool
            fallback = [
                variant
                for variant in variants
                if variant["id"] not in champion_ids and variant["id"] not in probation_ids
            ]
            if fallback:
                return fallback
        if pairing == "champion_vs_contender":
            pool = [
                variant
                for variant in variants
                if variant["id"] not in champion_ids
                and variant["id"] not in probation_ids
                and not is_under_sampled(variant, counts, settings, all_variants)
            ]
            if pool:
                return pool
            fallback = [
                variant
                for variant in variants
                if variant["id"] not in champion_ids and variant["id"] not in probation_ids
            ]
            if fallback:
                return fallback
        if pairing == "champion_vs_probation":
            if slot == "alpha":
                pool = [variant for variant in variants if variant["id"] in champion_ids]
            else:
                pool = [
                    variant
                    for variant in variants
                    if variant["id"] not in champion_ids and variant["id"] in probation_ids
                ]
            if pool:
                return pool
            if slot != "alpha":
                fallback = [variant for variant in variants if variant["id"] not in champion_ids]
                if fallback:
                    return fallback
    if round_payload.get("selection_mode") in {"scramble", "legacy"}:
        pairing = str(round_payload.get("selection_pairing") or "")
        if pairing == "scramble_weighted_vs_probation":
            if slot == "alpha":
                pool = [variant for variant in variants if variant["id"] not in probation_ids]
                if pool:
                    return pool
            if slot == "bravo":
                pool = [variant for variant in variants if variant["id"] in probation_ids]
                if pool:
                    return pool
        if pairing == "scramble_weighted":
            pool = [variant for variant in variants if variant["id"] not in probation_ids]
            if pool:
                return pool
        if pairing == "scramble_weighted_best_available" and slot == "alpha":
            if variants:
                highest_sample_count = max(counts.get(variant["id"], 0) for variant in variants)
                pool = [variant for variant in variants if counts.get(variant["id"], 0) == highest_sample_count]
                if pool:
                    return pool
            raise ValueError(
                f"no compatible best-available reroll variants remain for {task_class} ({pairing}, {slot})"
            )
        if pairing == "scramble_exploration_vs_best_available" and slot == "bravo":
            preferred = [variant for variant in variants if variant["id"] not in probation_ids]
            if preferred:
                highest_sample_count = max(counts.get(variant["id"], 0) for variant in preferred)
                pool = [
                    variant for variant in preferred if counts.get(variant["id"], 0) == highest_sample_count
                ]
                if pool:
                    return pool
            if variants:
                highest_sample_count = max(counts.get(variant["id"], 0) for variant in variants)
                pool = [variant for variant in variants if counts.get(variant["id"], 0) == highest_sample_count]
                if pool:
                    return pool
        if slot == "alpha":
            pool = [
                variant
                for variant in variants
                if variant["id"] not in probation_ids
                and is_under_sampled(variant, counts, settings, all_variants)
            ]
            if pool:
                return pool
            pool = [variant for variant in variants if variant["id"] not in probation_ids]
            if pool:
                return pool
        if slot == "bravo":
            if pairing == "scramble_exploration_vs_best_available":
                preferred = [variant for variant in variants if variant["id"] not in probation_ids]
                if preferred:
                    highest_sample_count = max(counts.get(variant["id"], 0) for variant in preferred)
                    pool = [
                        variant for variant in preferred if counts.get(variant["id"], 0) == highest_sample_count
                    ]
                    if pool:
                        return pool
            pool = [
                variant
                for variant in variants
                if not is_under_sampled(variant, counts, settings, all_variants) and variant["id"] not in probation_ids
            ]
            if pool:
                return pool
            pool = [variant for variant in variants if not is_under_sampled(variant, counts, settings, all_variants)]
            if pool:
                return pool
    return variants


def select_replacement_variant(
    *,
    roster: dict[str, Any],
    operational_state: dict[str, Any],
    records: list[dict[str, Any]],
    task_class: str,
    excluded_variant_ids: set[str],
    seed: int | None,
) -> dict[str, Any]:
    settings = roster.get("settings", {})
    all_variants = [
        variant
        for variant in eligible_variants(roster, task_class)
        if variant["id"] not in excluded_variant_ids
    ]
    cooling = _active_cooldowns(operational_state, task_class)
    variants = [variant for variant in all_variants if variant["id"] not in cooling]
    if not variants:
        if all_variants and cooling:
            raise ValueError(_cooldown_unavailability_message(task_class=task_class, cooling=cooling, needed=1))
        raise ValueError(f"no eligible replacement variants remain for {task_class}")
    counts = summarize_counts(records, task_class)
    return pick_weighted_without_replacement(
        variants,
        counts,
        settings,
        1,
        seed,
        relative_target=relative_underuse_target(all_variants, counts, settings),
    )[0]


def build_reroll_slot_payload(
    *,
    round_payload: dict[str, Any],
    roster: dict[str, Any],
    operational_state: dict[str, Any],
    records: list[dict[str, Any]],
    slot: str,
    seed: int | None,
) -> dict[str, Any]:
    if round_payload.get("status") != "completed":
        raise ValueError(f"round {round_payload['round_id']} must be completed before rerolling a slot")
    slot = str(slot).strip().lower()
    indexed_runs = {str(run["slot"]): run for run in round_payload.get("runs", [])}
    if slot not in indexed_runs:
        raise ValueError(f"unknown slot for round {round_payload['round_id']}: {slot}")
    if slot not in {"alpha", "bravo"}:
        raise ValueError("reroll-slot currently supports alpha or bravo only")
    replacement_source = indexed_runs[slot]
    survivor_slot = "bravo" if slot == "alpha" else "alpha"
    survivor = indexed_runs.get(survivor_slot)
    if survivor is None:
        raise ValueError(f"round {round_payload['round_id']} is missing {survivor_slot}")
    excluded_variant_ids: set[str] = set()
    if not bool(replacement_source.get("grade_blocked")):
        excluded_variant_ids.add(str(replacement_source["variant_id"]))
    if survivor.get("variant_id"):
        excluded_variant_ids.add(str(survivor["variant_id"]))
    replacement_candidates = _reroll_candidate_variants(
        roster=roster,
        operational_state=operational_state,
        records=records,
        round_payload=round_payload,
        slot=slot,
        excluded_variant_ids=excluded_variant_ids,
    )
    if not replacement_candidates:
        raise ValueError(f"no eligible replacement variants remain for {round_payload['task_class']}")
    counts = summarize_counts(records, str(round_payload["task_class"]))
    settings = roster.get("settings", {})
    reference_variants = [
        variant
        for variant in eligible_variants(roster, str(round_payload["task_class"]))
        if variant["id"] not in excluded_variant_ids
    ]
    champion_id_set = set(str(item) for item in (round_payload.get("selection_champion_variant_ids") or []))
    if (
        round_payload.get("selection_mode") == "champion"
        and (slot == "alpha" or str(round_payload.get("selection_pairing") or "") == "champion_vs_champion")
        and replacement_candidates
        and all(variant["id"] not in champion_id_set for variant in replacement_candidates)
    ):
        replacement_variant = replacement_candidates[0]
    else:
        replacement_variant = pick_weighted_without_replacement(
            replacement_candidates,
            counts,
            settings,
            1,
            seed,
            relative_target=relative_underuse_target(reference_variants, counts, settings),
        )[0]
    survivor_run = _finalized_run_summary(survivor)
    replacement_run = {
        "slot": slot,
        "variant_id": replacement_variant["id"],
        "model": replacement_variant["model"],
        "reasoning_effort": replacement_variant["reasoning_effort"],
        "rerolled_from_round_id": round_payload["round_id"],
        "rerolled_from_variant_id": replacement_source["variant_id"],
    }
    runs_by_slot = {
        slot: replacement_run,
        survivor_slot: survivor_run,
    }
    return {
        "round_id": make_round_id(
            str(round_payload["task_class"]),
            review_cwd=Path(str(round_payload.get("review_cwd"))) if round_payload.get("review_cwd") else None,
        ),
        "task_class": round_payload["task_class"],
        "selection_mode": round_payload.get("selection_mode"),
        "selection_pairing": round_payload.get("selection_pairing"),
        "selection_champion_variant_ids": list(round_payload.get("selection_champion_variant_ids") or []),
        "selection_anchor_kind": (
            (
                "champion"
                if replacement_variant["id"] in champion_id_set
                else "acting_champion"
            )
            if round_payload.get("selection_mode") == "champion" and slot == "alpha"
            else round_payload.get("selection_anchor_kind")
        ),
        "selection_fallback_reason": (
            (
                None
                if replacement_variant["id"] in champion_id_set
                else "champion_pool_unavailable"
            )
            if round_payload.get("selection_mode") == "champion" and slot == "alpha"
            else round_payload.get("selection_fallback_reason")
        ),
        "sampled_at": utc_now_iso(),
        "caller_id": round_payload.get("caller_id"),
        "caller_id_source": round_payload.get("caller_id_source"),
        "review_cwd_normalized": round_payload.get("review_cwd_normalized"),
        "excluded_variant_ids": sorted(excluded_variant_ids),
        "status": "sampled",
        "runs": [runs_by_slot["alpha"], runs_by_slot["bravo"]],
        "rerolled_from_round_id": round_payload["round_id"],
        "rerolled_slot": slot,
    }


def write_round(state_dir: Path, payload: dict[str, Any]) -> Path:
    path = round_path(state_dir, payload["round_id"])
    with state_lock(state_dir, f"round-{payload['round_id']}"):
        write_json(path, payload)
    return path


def load_round(state_dir: Path, round_id: str) -> dict[str, Any]:
    path = round_path(state_dir, round_id)
    if not path.exists():
        raise ValueError(f"unknown round_id: {round_id}")
    return read_json(path)


def append_record_if_new(state_dir: Path, record: dict[str, Any]) -> bool:
    with state_lock(state_dir, "runs"):
        record = compact_benchmark_record(record)
        existing_records = read_jsonl(state_dir / RUN_LOG_FILENAME)
        identity = record_identity_key(record)
        if any(record_identity_key(existing) == identity for existing in existing_records):
            return False
        append_jsonl(state_dir / RUN_LOG_FILENAME, record)
        return True


def normalize_grade_basis(value: str, rubric: dict[str, Any]) -> str:
    basis = str(value or "").strip().lower()
    allowed = set(str(item) for item in list(rubric.get("basis") or GRADE_BASIS_VALUES))
    if basis not in allowed:
        raise ValueError(f"basis must be one of: {', '.join(sorted(allowed))}")
    return basis


def build_review_command(
    *,
    model: str,
    reasoning_effort: str,
    service_tier: str | None = None,
    title: str,
    review_scope: dict[str, Any],
    review_cwd: Path,
    prompt: str = "",
    allow_unsafe_windows_wsl_fallback: bool = False,
) -> list[str]:
    codex_executable = shutil.which("codex") or shutil.which("codex.cmd") or "codex"
    _validate_wsl_codex_runtime(
        codex_executable,
        review_cwd=review_cwd,
        allow_unsafe_windows_wsl_fallback=allow_unsafe_windows_wsl_fallback,
    )
    base_branch = review_scope.get("base") if uses_native_base_review(review_scope) else None
    if _use_unsafe_windows_wsl_fallback(review_cwd, allow_unsafe_windows_wsl_fallback):
        command = [
            codex_executable,
            "exec",
            "-C",
            str(review_cwd),
            "review",
            "--dangerously-bypass-approvals-and-sandbox",
            "-c",
            f'model="{model}"',
            "-c",
            f'model_reasoning_effort="{reasoning_effort}"',
            "--title",
            title,
        ]
    else:
        command = [
            codex_executable,
            "review",
            "-c",
            f'model="{model}"',
            "-c",
            f'model_reasoning_effort="{reasoning_effort}"',
            "--title",
            title,
        ]
    normalized_service_tier = normalize_service_tier(service_tier)
    if normalized_service_tier:
        insert_at = command.index("--title")
        command[insert_at:insert_at] = ["-c", f'service_tier="{normalized_service_tier}"']
    if base_branch:
        command.extend(["--base", str(base_branch)])
    else:
        if not prompt.strip():
            raise ValueError("manual review mode requires a non-empty custom prompt")
        command.append("-")
    return command


def ensure_clean_git_worktree(review_cwd: Path, *, allow_dirty: bool, review_scope: dict[str, Any] | None = None) -> None:
    try:
        dirty_entries = meaningful_worktree_status_entries(review_cwd)
    except ValueError as exc:
        raise ValueError(f"review-suite requires a git repo with committed changes ready for review: {exc}") from exc
    if not dirty_entries:
        return
    if allow_dirty:
        print(
            "[review-suite] WARNING: dirty worktree override enabled; results are not clean committed-scope benchmark input.",
            file=sys.stderr,
            flush=True,
        )
        return
    scope = dict(review_scope or {})
    base = str(scope.get("base") or "").strip()
    if base and not str(scope.get("commit") or "").strip() and not str(scope.get("commit_end") or "").strip():
        dirty_scope = dirty_worktree_scope(
            review_cwd,
            base,
            merge_base_ref=str(scope.get("merge_base") or "").strip() or None,
        )
        if bool(dirty_scope.get("all_dirty_paths_outside_branch_diff")):
            return
    raise ValueError(
        "review-suite requires a clean worktree. Commit the slice before running a review round, or pass --allow-dirty to override."
    )
def _use_unsafe_windows_wsl_fallback(review_cwd: Path, allow_unsafe_windows_wsl_fallback: bool) -> bool:
    return use_unsafe_windows_wsl_fallback(review_cwd, allow_unsafe_windows_wsl_fallback)


def _validate_wsl_codex_runtime(
    codex_executable: str,
    *,
    review_cwd: Path,
    allow_unsafe_windows_wsl_fallback: bool,
) -> None:
    validate_codex_runtime(
        tool_name="review-suite",
        codex_executable=codex_executable,
        review_root=review_cwd,
        allow_unsafe_windows_wsl_fallback=allow_unsafe_windows_wsl_fallback,
        unsafe_command_hint="codex exec review --dangerously-bypass-approvals-and-sandbox",
    )


def extract_session_id(text: str) -> str | None:
    marker = "session id:"
    for line in text.splitlines():
        if marker not in line.lower():
            continue
        _, _, tail = line.partition(":")
        candidate = tail.strip()
        if candidate:
            return candidate
    return None


def _started_at_epoch_seconds(value: Any) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _normalize_review_text(value: str) -> str:
    return " ".join((value or "").strip().split()).lower()


def _capacity_interruption_detected(*, stderr_text: str, reviewer_output: str) -> bool:
    haystack = f"{stderr_text}\n{reviewer_output}".lower()
    return "selected model is at capacity" in haystack


def _review_interrupted_detected(*, stderr_text: str, reviewer_output: str) -> bool:
    output_first_line = _first_nonempty_line(reviewer_output)
    if output_first_line:
        return _normalize_review_text(output_first_line).startswith("review was interrupted")
    return "review was interrupted" in (stderr_text or "").lower()


def _tooling_failure_detected(*, stderr_text: str, reviewer_output: str) -> bool:
    output_haystack = (reviewer_output or "").lower()
    stderr_haystack = (stderr_text or "").lower()
    output_markers = (
        "i could not inspect the changes because shell access failed",
        "without a readable diff",
        "cannot identify verifiable, actionable defects, so no findings are reported",
    )
    if any(marker in output_haystack for marker in output_markers):
        return True
    if not output_haystack.strip():
        return "windows sandbox: setup refresh failed" in stderr_haystack
    return False


def _first_nonempty_line(value: str) -> str | None:
    for line in (value or "").splitlines():
        text = line.strip()
        if text:
            return text
    return None


def _review_output_summary(reviewer_output: str) -> str | None:
    first_line = _first_nonempty_line(reviewer_output)
    if not first_line:
        return None
    if len(first_line) <= 220:
        return first_line
    return f"{first_line[:217]}..."


def _classify_review_result(
    *,
    reviewer_output: str,
    stderr_text: str,
    session_id: str | None,
    thread_id: str | None,
) -> dict[str, Any]:
    output = (reviewer_output or "").strip()
    capacity = _capacity_interruption_detected(stderr_text=stderr_text, reviewer_output=output)
    interrupted = _review_interrupted_detected(stderr_text=stderr_text, reviewer_output=output)
    if capacity:
        return {
            "review_status": "interrupted_capacity",
            "status_summary": "selected_model_at_capacity",
            "grade_blocked": True,
            "grade_block_reason": "selected_model_at_capacity",
        }
    if _tooling_failure_detected(stderr_text=stderr_text, reviewer_output=output):
        return {
            "review_status": "tooling_failure",
            "status_summary": "review_tooling_failure",
            "grade_blocked": True,
            "grade_block_reason": "review_tooling_failure",
        }
    if interrupted:
        return {
            "review_status": "interrupted",
            "status_summary": "Review was interrupted before a usable result was captured. Do not grade this against the model.",
            "grade_blocked": True,
            "grade_block_reason": "review_interrupted",
        }
    if output:
        return {
            "review_status": "completed",
            "status_summary": _review_output_summary(output) or "Review completed.",
            "grade_blocked": False,
            "grade_block_reason": None,
        }
    if session_id or thread_id:
        return {
            "review_status": "completed_no_output",
            "status_summary": "Reviewer session finished, but no review text was captured.",
            "grade_blocked": True,
            "grade_block_reason": "missing_reviewer_output",
        }
    return {
        "review_status": "process_died",
        "status_summary": "Reviewer process exited without a captured result.",
        "grade_blocked": True,
        "grade_block_reason": "reviewer_process_exited",
    }


def classify_review_capture(
    *,
    reviewer_output: str,
    stderr_text: str,
    session_id: str | None,
    thread_id: str | None,
    timed_out: bool = False,
    transport_stalled: bool = False,
) -> dict[str, Any]:
    if transport_stalled:
        return {
            "review_status": "transport_stalled",
            "status_summary": "Reviewer transport stalled after reconnect exhaustion before a usable result was captured.",
            "grade_blocked": True,
            "grade_block_reason": "review_transport_stalled",
        }
    if timed_out:
        return {
            "review_status": "timeout",
            "status_summary": (
                "Reviewer timed out after partial output was captured. Do not treat this as a completed review."
                if str(reviewer_output or "").strip()
                else "Reviewer timed out before a usable result was captured."
            ),
            "grade_blocked": True,
            "grade_block_reason": "review_timed_out",
        }
    return _classify_review_result(
        reviewer_output=reviewer_output,
        stderr_text=stderr_text,
        session_id=session_id,
        thread_id=thread_id,
    )


def _classification_for_run(run: dict[str, Any]) -> dict[str, Any]:
    existing_status = run.get("review_status")
    if existing_status:
        if existing_status in {"completed", "completed_findings", "completed_no_findings"} and _tooling_failure_detected(
            stderr_text=str(run.get("stderr") or ""),
            reviewer_output=str(run.get("reviewer_output") or ""),
        ):
            return _classify_review_result(
                reviewer_output=str(run.get("reviewer_output") or ""),
                stderr_text=str(run.get("stderr") or ""),
                session_id=str(run.get("session_id") or "") or None,
                thread_id=str(run.get("thread_id") or "") or None,
            )
        if existing_status in {"completed", "completed_findings", "completed_no_findings"}:
            return {
                "review_status": "completed",
                "status_summary": run.get("status_summary") or _review_output_summary(str(run.get("reviewer_output") or "")) or "Review completed.",
                "grade_blocked": False,
                "grade_block_reason": None,
            }
        return {
            "review_status": existing_status,
            "status_summary": run.get("status_summary"),
            "grade_blocked": bool(run.get("grade_blocked")),
            "grade_block_reason": run.get("grade_block_reason"),
        }
    return _classify_review_result(
        reviewer_output=str(run.get("reviewer_output") or ""),
        stderr_text=str(run.get("stderr") or ""),
        session_id=str(run.get("session_id") or "") or None,
        thread_id=str(run.get("thread_id") or "") or None,
    )


def collect_completed_review_capture(
    *,
    slot: str,
    variant_id: str,
    variant: dict[str, Any],
    title: str,
    command: list[str] | None,
    stdout_path: Path,
    stderr_path: Path,
    started_at: str | None,
    sqlite_path: Path,
    review_cwd: Path,
    timed_out: bool = False,
    transport_stalled: bool = False,
) -> dict[str, Any]:
    stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace").strip() if stderr_path.exists() else ""
    reviewer_output = stdout_path.read_text(encoding="utf-8", errors="replace").strip() if stdout_path.exists() else ""
    session_id = extract_session_id(stderr_text)
    review_cwd_text = str(review_cwd)
    thread = None
    launcher_thread = None
    started_after = _started_at_epoch_seconds(started_at)
    created_after = max(0, started_after - 5) if started_after is not None else int(time.time()) - 5
    if session_id:
        for attempt in range(6):
            candidate = find_thread_by_id(sqlite_path=sqlite_path, thread_id=session_id)
            if candidate:
                if str(candidate.get("source") or "") == REVIEW_SUBAGENT_SOURCE:
                    thread = candidate
                    break
                launcher_thread = candidate
                review_child_thread = find_review_child_thread(
                    sqlite_path=sqlite_path,
                    cwd=str(candidate.get("cwd") or review_cwd_text),
                    title=str(candidate.get("title") or ""),
                    model=str(variant["model"]),
                    reasoning_effort=str(variant["reasoning_effort"]),
                    created_at=candidate.get("created_at"),
                    updated_at=candidate.get("updated_at"),
                )
                if review_child_thread:
                    thread = review_child_thread
                    break
            if attempt < 5:
                time.sleep(0.5)
    if not thread:
        for attempt in range(6):
            thread = find_review_child_thread(
                sqlite_path=sqlite_path,
                cwd=review_cwd_text,
                model=str(variant["model"]),
                reasoning_effort=str(variant["reasoning_effort"]),
                created_at=started_after,
            )
            if thread:
                break
            if attempt < 5:
                time.sleep(0.5)
    if not thread:
        for attempt in range(6):
            candidate = find_thread_by_title(
                sqlite_path=sqlite_path,
                title=title,
                cwd=review_cwd_text,
                created_after=created_after,
            )
            if candidate:
                if str(candidate.get("source") or "") == REVIEW_SUBAGENT_SOURCE:
                    thread = candidate
                    break
                launcher_thread = candidate
                review_child_thread = find_review_child_thread(
                    sqlite_path=sqlite_path,
                    cwd=str(candidate.get("cwd") or review_cwd_text),
                    title=str(candidate.get("title") or ""),
                    model=str(variant["model"]),
                    reasoning_effort=str(variant["reasoning_effort"]),
                    created_at=candidate.get("created_at"),
                    updated_at=candidate.get("updated_at"),
                )
                if review_child_thread:
                    thread = review_child_thread
                    break
            if attempt < 5:
                time.sleep(0.5)
    if not thread and launcher_thread:
        thread = launcher_thread
    enriched = enrich_thread_record(thread) if thread else {}
    if not reviewer_output and enriched.get("reviewer_output"):
        reviewer_output = enriched["reviewer_output"]
    classification = classify_review_capture(
        reviewer_output=reviewer_output,
        stderr_text=stderr_text,
        session_id=session_id,
        thread_id=str(enriched.get("id") or "") or None,
        timed_out=timed_out,
        transport_stalled=transport_stalled,
    )
    elapsed_seconds = None
    if started_at:
        elapsed_seconds = round((utc_now() - datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))).total_seconds(), 3)
    return {
        "slot": slot,
        "variant_id": variant_id,
        "service_tier": variant_service_tier(variant),
        "title": title,
        "command": command,
        "returncode": 0 if session_id or reviewer_output else None,
        "elapsed_seconds": elapsed_seconds,
        "session_id": session_id,
        "thread_id": enriched.get("id"),
        "rollout_path": enriched.get("rollout_path"),
        "tokens_used": enriched.get("tokens_used"),
        "usage": enriched.get("usage", {}),
        "cost_usd": compute_cost_usd(variant, enriched.get("usage", {})),
        "reviewer_output": reviewer_output,
        "stderr": stderr_text,
        "review_status": classification["review_status"],
        "status_summary": classification["status_summary"],
        "grade_blocked": classification["grade_blocked"],
        "grade_block_reason": classification["grade_block_reason"],
        "reviewer_output_ref": (
            f"rollout://{enriched['id']}/{variant_id}" if enriched.get("id") else None
        ),
    }


def _load_live_run_artifacts(item: dict[str, Any]) -> tuple[str, str]:
    stderr_path = Path(str(item["stderr_path"]))
    stdout_path = Path(str(item["stdout_path"]))
    stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace").strip() if stderr_path.exists() else ""
    reviewer_output = stdout_path.read_text(encoding="utf-8", errors="replace").strip() if stdout_path.exists() else ""
    return stderr_text, reviewer_output


def _summarize_live_run(item: dict[str, Any]) -> dict[str, Any]:
    stderr_text, reviewer_output = _load_live_run_artifacts(item)
    summary = _classify_review_result(
        reviewer_output=reviewer_output,
        stderr_text=stderr_text,
        session_id=None,
        thread_id=None,
    )
    summary["stderr"] = stderr_text
    summary["reviewer_output"] = reviewer_output
    return summary


def _apply_capacity_cooldowns(*, state_dir: Path, round_payload: dict[str, Any]) -> list[dict[str, Any]]:
    updates: list[dict[str, Any]] = []
    with state_lock(state_dir, "operational-state"):
        state_path = state_dir / OPERATIONAL_STATE_FILENAME
        operational_state = load_operational_state(state_path)
        task_state = operational_state["task_classes"][str(round_payload["task_class"])]
        cooldowns = dict(task_state.get("cooldowns") or {})
        changed = False
        now = utc_now()
        now_iso = now.isoformat().replace("+00:00", "Z")
        triggered_variants: set[str] = set()
        completed_variants: set[str] = set()
        for run in round_payload.get("runs", []):
            classification = _classification_for_run(run)
            variant_id = str(run.get("variant_id") or "")
            if not variant_id:
                continue
            block_reason = str(classification.get("grade_block_reason") or "")
            if block_reason in COOLDOWN_BLOCK_REASONS or (
                bool(run.get("cooldown_eligible")) and block_reason in MARKED_COOLDOWN_BLOCK_REASONS
            ):
                triggered_variants.add(variant_id)
                current = cooldowns.get(variant_id) or {}
                failure_count = int(current.get("failure_count", 0) or 0) + 1
                until = now + timedelta(seconds=_capacity_cooldown_seconds(failure_count))
                cooldowns[variant_id] = {
                    "until": until.isoformat().replace("+00:00", "Z"),
                    "failure_count": failure_count,
                    "last_reason": block_reason,
                    "last_triggered_at": now_iso,
                }
                updates.append(
                    {
                        "variant_id": variant_id,
                        "reason": block_reason,
                        "failure_count": failure_count,
                        "until": cooldowns[variant_id]["until"],
                    }
                )
                changed = True
                continue
            if classification["review_status"] == "completed":
                completed_variants.add(variant_id)
        for variant_id in completed_variants - triggered_variants:
            if variant_id in cooldowns:
                cooldowns.pop(variant_id, None)
                changed = True
        if not changed:
            return updates
        task_state["cooldowns"] = cooldowns
        operational_state["generated_at"] = utc_now_iso()
        _prune_expired_cooldowns(operational_state)
        write_json(state_path, operational_state)
    return updates


def build_manual_review_prompt(*, instructions: str, diff_text: str) -> str:
    instruction_text = instructions.strip()
    diff_body = diff_text.strip()
    if not instruction_text:
        raise ValueError("manual review mode requires custom instructions")
    if not diff_body:
        raise ValueError("manual review mode requires a non-empty diff artifact")
    return (
        "You are reviewing a manually supplied diff artifact.\n\n"
        "Follow these custom instructions exactly:\n"
        f"{instruction_text}\n\n"
        "Use only the diff below as the review artifact.\n"
        "Reviewer output is advisory risk input, not authoritative product direction. "
        "Report only findings that identify concrete correctness, regression, integration, security, accessibility, or maintainability risk against stated requirements, docs, code invariants, or explicit contracts. "
        "UX preferences, product-scope speculation, backwards-compat speculation, and alternative product direction are non-findings. "
        "Put unresolved scope questions or conflicts with explicit user/product direction under `Scope questions / suggestions (non-findings)` with the tradeoff to escalate. "
        "Do not recommend code changes that reverse explicit product intent. "
        "Focused review-relevant checks can be enough to launch the next review round; full-suite/CI remains a merge-readiness requirement. "
        "Record full-suite/CI validation status when relevant as pending, passed, failed, or intentionally waived/classified; do not call a PR final or merge-ready while that status is unknown. "
        "If there are no correctness issues, say 'No findings.'\n\n"
        "=== BEGIN DIFF ===\n"
        f"{diff_body}\n"
        "=== END DIFF ==="
    )


def _review_run_title(*, round_id: str, slot: str, variant_id: str, capacity_retry_attempts: int = 0) -> str:
    title = f"review-suite::{round_id}::{slot}::{variant_id}"
    if capacity_retry_attempts <= 0:
        return title
    return f"{title}::retry{capacity_retry_attempts}"


def _launch_reviewer_process(
    *,
    round_payload: dict[str, Any],
    run: dict[str, Any],
    variant: dict[str, Any],
    review_cwd: Path,
    prompt: str,
    review_scope: dict[str, Any],
    allow_unsafe_windows_wsl_fallback: bool,
) -> dict[str, Any]:
    manual_prompt = prompt if not uses_native_base_review(review_scope) else ""
    retry_attempts = int(run.get("capacity_retry_attempts", 0) or 0)
    title = _review_run_title(
        round_id=str(round_payload["round_id"]),
        slot=str(run["slot"]),
        variant_id=str(run["variant_id"]),
        capacity_retry_attempts=retry_attempts,
    )
    stdout_tmp = tempfile.NamedTemporaryFile(prefix=f"review-suite-{run['slot']}-", suffix=".stdout.txt", delete=False)
    stderr_tmp = tempfile.NamedTemporaryFile(prefix=f"review-suite-{run['slot']}-", suffix=".stderr.txt", delete=False)
    stdout_path = Path(stdout_tmp.name)
    stderr_path = Path(stderr_tmp.name)
    command = build_review_command(
        model=variant["model"],
        reasoning_effort=variant["reasoning_effort"],
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
            if _use_unsafe_windows_wsl_fallback(review_cwd, allow_unsafe_windows_wsl_fallback)
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
    run.update(
        {
            "title": title,
            "command": command,
            "service_tier": variant_service_tier(variant),
            "pid": proc.pid,
            "started_at": utc_now_iso(),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }
    )
    return run


def _cleanup_run_artifacts(run: dict[str, Any]) -> None:
    for path_key in ("stdout_path", "stderr_path"):
        path_value = str(run.get(path_key) or "").strip()
        if not path_value:
            continue
        try:
            Path(path_value).unlink(missing_ok=True)
        except OSError:
            pass


def _strip_live_run_transient_fields(run: dict[str, Any]) -> dict[str, Any]:
    cleaned = deepcopy(run)
    for key in ("pid", "stdout_path", "stderr_path"):
        cleaned.pop(key, None)
    return cleaned


def _collect_completed_run_from_artifacts(
    *,
    item: dict[str, Any],
    indexed: dict[str, dict[str, Any]],
    sqlite_path: Path,
    review_cwd: Path,
) -> dict[str, Any]:
    variant_id = str(item["variant_id"])
    return collect_completed_review_capture(
        slot=str(item["slot"]),
        variant_id=variant_id,
        variant=indexed[variant_id],
        title=str(item["title"]),
        command=list(item.get("command") or []),
        stdout_path=Path(str(item["stdout_path"])),
        stderr_path=Path(str(item["stderr_path"])),
        started_at=str(item.get("started_at") or "") or None,
        sqlite_path=sqlite_path,
        review_cwd=review_cwd,
    )


def _maybe_retry_capacity_run(
    *,
    round_payload: dict[str, Any],
    run: dict[str, Any],
    indexed: dict[str, dict[str, Any]],
    state_dir: Path,
    review_cwd: Path,
) -> bool:
    summary = _summarize_live_run(run)
    if summary.get("grade_block_reason") != "selected_model_at_capacity":
        return False
    retry_attempts = int(run.get("capacity_retry_attempts", 0) or 0)
    if retry_attempts >= CAPACITY_RETRY_MAX_ATTEMPTS:
        return False
    next_attempt = retry_attempts + 1
    print(
        f"[review-suite] {public_reviewer_label(str(run['slot']))} hit capacity; retrying in {CAPACITY_RETRY_DELAY_SECONDS}s "
        f"(attempt {next_attempt}/{CAPACITY_RETRY_MAX_ATTEMPTS})",
        file=sys.stderr,
        flush=True,
    )
    _cleanup_run_artifacts(run)
    run["capacity_retry_attempts"] = next_attempt
    for transient_key in (
        "pid",
        "session_id",
        "thread_id",
        "rollout_path",
        "tokens_used",
        "usage",
        "reviewer_output",
        "stderr",
        "review_status",
        "status_summary",
        "grade_blocked",
        "grade_block_reason",
        "reviewer_output_ref",
        "returncode",
        "elapsed_seconds",
        "service_tier",
    ):
        run.pop(transient_key, None)
    time.sleep(CAPACITY_RETRY_DELAY_SECONDS)
    _launch_reviewer_process(
        round_payload=round_payload,
        run=run,
        variant=indexed[str(run["variant_id"])],
        review_cwd=review_cwd,
        prompt=str(round_payload.get("requested_prompt") or ""),
        review_scope=deepcopy(round_payload.get("review_scope") or {}),
        allow_unsafe_windows_wsl_fallback=bool(round_payload.get("allow_unsafe_windows_wsl_fallback")),
    )
    write_round(state_dir, round_payload)
    return True


def run_round(
    *,
    round_payload: dict[str, Any],
    roster: dict[str, Any],
    state_dir: Path,
    review_cwd: Path,
    prompt: str,
    review_scope: dict[str, Any],
    sqlite_path: Path = DEFAULT_SQLITE_STATE_PATH,
    progress_interval_seconds: int = 30,
    allow_dirty: bool = False,
    allow_unsafe_windows_wsl_fallback: bool = False,
) -> dict[str, Any]:
    launched = launch_round(
        round_payload=round_payload,
        roster=roster,
        state_dir=state_dir,
        review_cwd=review_cwd,
        prompt=prompt,
        review_scope=review_scope,
        progress_interval_seconds=progress_interval_seconds,
        allow_dirty=allow_dirty,
        allow_unsafe_windows_wsl_fallback=allow_unsafe_windows_wsl_fallback,
    )
    return collect_round_results(
        round_payload=launched,
        roster=roster,
        state_dir=state_dir,
        review_cwd=review_cwd,
        sqlite_path=sqlite_path,
        progress_interval_seconds=progress_interval_seconds,
        wait=True,
    )


def launch_round(
    *,
    round_payload: dict[str, Any],
    roster: dict[str, Any],
    state_dir: Path,
    review_cwd: Path,
    prompt: str,
    review_scope: dict[str, Any],
    progress_interval_seconds: int = 30,
    allow_dirty: bool = False,
    allow_unsafe_windows_wsl_fallback: bool = False,
) -> dict[str, Any]:
    indexed = variant_index(roster)
    review_cwd = review_cwd.resolve()
    if round_payload.get("status") not in {"sampled", "failed"}:
        raise ValueError(f"round {round_payload['round_id']} is already {round_payload.get('status')}")
    if review_scope.get("base"):
        ensure_clean_git_worktree(review_cwd, allow_dirty=allow_dirty, review_scope=review_scope)

    review_started_at = utc_now_iso()
    running_payload = deepcopy(round_payload)
    running_payload["status"] = "running"
    running_payload["review_cwd"] = str(review_cwd)
    running_payload["review_started_at"] = review_started_at
    running_payload["review_scope"] = review_scope
    running_payload["allow_dirty"] = allow_dirty
    running_payload["allow_unsafe_windows_wsl_fallback"] = allow_unsafe_windows_wsl_fallback
    running_payload["progress_interval_seconds"] = progress_interval_seconds
    if _use_unsafe_windows_wsl_fallback(review_cwd, allow_unsafe_windows_wsl_fallback):
        print(
            "[review-suite] WARNING: using Windows Codex fallback for a WSL UNC repo. This bypasses the Codex sandbox and is not the happy path.",
            file=sys.stderr,
            flush=True,
        )
    for run in round_payload["runs"]:
        if _run_is_finalized(run):
            continue
        variant = indexed[run["variant_id"]]
        _launch_reviewer_process(
            round_payload=round_payload,
            run=run,
            variant=variant,
            review_cwd=review_cwd,
            prompt=prompt,
            review_scope=review_scope,
            allow_unsafe_windows_wsl_fallback=allow_unsafe_windows_wsl_fallback,
        )
        running_run = deepcopy(run)
        running_payload["runs"] = [
            running_run if existing["slot"] == running_run["slot"] else deepcopy(existing)
            for existing in running_payload["runs"]
        ]
        write_round(state_dir, running_payload)
    if prompt:
        running_payload["requested_prompt"] = prompt
    if not any(run.get("pid") for run in running_payload["runs"] if not _run_is_finalized(run)):
        running_payload["status"] = "completed"
        running_payload["review_completed_at"] = utc_now_iso()
    write_round(state_dir, running_payload)
    return running_payload


def _process_is_running(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        proc = subprocess.run(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if proc.returncode != 0:
            return False
        output = (proc.stdout or "").strip()
        if not output or output.startswith("INFO:"):
            return False
        return f'"{int(pid)}"' in output or f",{int(pid)}," in output
    try:
        with open(f"/proc/{int(pid)}/status", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.startswith("State:"):
                    continue
                if "\tZ" in line or "zombie" in line.lower():
                    return False
                break
    except FileNotFoundError:
        return False
    except OSError:
        pass
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except OSError:
        return False
    except PermissionError:
        return True
    return True


def round_has_live_reviewer_process(round_payload: dict[str, Any]) -> bool:
    for run in list(round_payload.get("runs") or []):
        if _run_is_finalized(run):
            continue
        if _process_is_running(run.get("pid")):
            return True
    return False


def collect_round_results(
    *,
    round_payload: dict[str, Any],
    roster: dict[str, Any],
    state_dir: Path,
    review_cwd: Path,
    sqlite_path: Path = DEFAULT_SQLITE_STATE_PATH,
    progress_interval_seconds: int = 30,
    wait: bool = True,
) -> dict[str, Any]:
    indexed = variant_index(roster)
    if round_payload.get("status") != "running":
        raise ValueError(f"round {round_payload['round_id']} is not running")

    last_progress = time.monotonic()
    announced_terminal_states: set[str] = set()
    stall_warned_slots: set[str] = set()
    live_completion_statuses: dict[str, str] = {}
    print(
        "[review-suite] waiting for both reviewers; completion status prints as each finishes.",
        file=sys.stderr,
        flush=True,
    )
    while True:
        alive = [run for run in round_payload["runs"] if _process_is_running(run.get("pid"))]
        restarted_capacity_run = False
        for item in round_payload["runs"]:
            slot = str(item["slot"])
            if _run_is_finalized(item):
                announced_terminal_states.add(slot)
                continue
            if not item.get("stderr_path") or not item.get("stdout_path"):
                announced_terminal_states.add(slot)
                continue
            if any(alive_item["slot"] == slot for alive_item in alive):
                continue
            if slot in announced_terminal_states:
                continue
            if _maybe_retry_capacity_run(
                round_payload=round_payload,
                run=item,
                indexed=indexed,
                state_dir=state_dir,
                review_cwd=review_cwd,
            ):
                announced_terminal_states.discard(slot)
                last_progress = time.monotonic()
                restarted_capacity_run = True
                break
            terminal_summary = (
                collect_completed_review_capture(
                    slot=str(item["slot"]),
                    variant_id=str(item["variant_id"]),
                    variant=indexed[str(item["variant_id"])],
                    title=str(item["title"]),
                    command=list(item.get("command") or []),
                    stdout_path=Path(str(item["stdout_path"])),
                    stderr_path=Path(str(item["stderr_path"])),
                    started_at=str(item.get("started_at") or "") or None,
                    sqlite_path=sqlite_path,
                    review_cwd=review_cwd,
                    transport_stalled=True,
                )
                if bool(item.get("transport_stalled"))
                else _collect_completed_run_from_artifacts(
                    item=item,
                    indexed=indexed,
                    sqlite_path=sqlite_path,
                    review_cwd=review_cwd,
                )
            )
            item.update(terminal_summary)
            write_round(state_dir, round_payload)
            _print_live_completed_run(terminal_summary)
            live_completion_statuses[slot] = reviewer_completion_status(terminal_summary)
            announced_terminal_states.add(slot)
        if restarted_capacity_run:
            continue
        if not alive:
            break
        if not wait:
            return round_payload
        now = time.monotonic()
        if now - last_progress >= progress_interval_seconds:
            _print_transport_events(alive)
            for item in list(alive):
                stall_reason = _transport_stalled(item)
                if stall_reason is None:
                    hang_reason = _transport_hung_after_output(item)
                    if hang_reason is None:
                        continue
                    label = public_reviewer_label(str(item.get("slot") or "reviewer"))
                    print(
                        f"[review-suite] {label} transport hung after output ({hang_reason}); stopping process and preserving captured output.",
                        file=sys.stderr,
                        flush=True,
                    )
                    _terminate_process_tree(item.get("pid"))
                else:
                    label = public_reviewer_label(str(item.get("slot") or "reviewer"))
                    print(
                        f"[review-suite] {label} transport stalled ({stall_reason}); stopping this reviewer and preserving the other reviewer output.",
                        file=sys.stderr,
                        flush=True,
                    )
                    item["transport_stalled"] = True
                    _terminate_process_tree(item.get("pid"))
            _print_stall_warnings(
                active_runs=alive,
                indexed=indexed,
                sqlite_path=sqlite_path,
                review_cwd=review_cwd,
                warned_slots=stall_warned_slots,
            )
            print(_progress_status_line(alive), file=sys.stderr, flush=True)
            last_progress = now
        time.sleep(1.0)

    completed_runs: list[dict[str, Any]] = []
    for item in round_payload["runs"]:
        if _run_is_finalized(item):
            completed_runs.append(_strip_live_run_transient_fields(_finalized_run_summary(item)))
            _cleanup_run_artifacts(item)
            continue
        if not item.get("stderr_path") or not item.get("stdout_path"):
            completed_runs.append(_strip_live_run_transient_fields(_finalized_run_summary(item)))
            continue
        completed_runs.append(
            collect_completed_review_capture(
                slot=str(item["slot"]),
                variant_id=str(item["variant_id"]),
                variant=indexed[str(item["variant_id"])],
                title=str(item["title"]),
                command=list(item.get("command") or []),
                stdout_path=Path(str(item["stdout_path"])),
                stderr_path=Path(str(item["stderr_path"])),
                started_at=str(item.get("started_at") or "") or None,
                sqlite_path=sqlite_path,
                review_cwd=review_cwd,
                transport_stalled=bool(item.get("transport_stalled")),
            )
            if bool(item.get("transport_stalled"))
            else _collect_completed_run_from_artifacts(
                item=item,
                indexed=indexed,
                sqlite_path=sqlite_path,
                review_cwd=review_cwd,
            )
        )
        _cleanup_run_artifacts(item)
    round_payload = deepcopy(round_payload)
    round_payload["status"] = "completed"
    round_payload["review_completed_at"] = utc_now_iso()
    round_payload["live_completion_statuses"] = dict(sorted(live_completion_statuses.items()))
    round_payload["runs"] = completed_runs
    cooldown_updates = _apply_capacity_cooldowns(state_dir=state_dir, round_payload=round_payload)
    if cooldown_updates:
        round_payload["cooldown_updates"] = cooldown_updates
        for update in cooldown_updates:
            print(
                f"[review-suite] cooling {update['variant_id']} for {round_payload['task_class']} until {format_cooldown_until_for_display(update['until'])} after capacity hit (failure_count={update['failure_count']})",
                file=sys.stderr,
                flush=True,
            )
    write_round(state_dir, round_payload)
    return round_payload


def record_identity_key(record: dict[str, Any]) -> str:
    identity_runs: list[dict[str, Any]] = []
    for run in sorted(record.get("runs", []), key=lambda item: item["variant_id"]):
        identity_runs.append(
            {
                "variant_id": run["variant_id"],
                "reviewer_output": run.get("reviewer_output"),
                "reviewer_output_ref": run.get("reviewer_output_ref"),
                "grader_notes": run.get("grader_notes"),
                "elapsed_seconds": run.get("elapsed_seconds"),
            }
        )
    return json.dumps(
        {
            "task_class": record.get("task_class"),
            "task_id": record.get("task_id"),
            "selection_mode": record.get("selection_mode"),
            "pairwise_outcome": record.get("pairwise_outcome", {}),
            "runs": identity_runs,
        },
        sort_keys=True,
    )


def compact_benchmark_run(run: dict[str, Any]) -> dict[str, Any]:
    compacted: dict[str, Any] = {
        "variant_id": run["variant_id"],
        "service_tier": run.get("service_tier"),
        "elapsed_seconds": run.get("elapsed_seconds"),
        "usage": deepcopy(run.get("usage", {})),
        "cost_usd": run.get("cost_usd"),
    }
    if not compacted["service_tier"]:
        compacted.pop("service_tier", None)
    reviewer_output = run.get("reviewer_output")
    if reviewer_output:
        compacted["reviewer_output"] = reviewer_output
    reviewer_output_ref = run.get("reviewer_output_ref")
    if reviewer_output_ref:
        compacted["reviewer_output_ref"] = reviewer_output_ref
    grader_notes = run.get("grader_notes")
    if grader_notes:
        compacted["grader_notes"] = grader_notes
    return compacted


def compact_benchmark_record(record: dict[str, Any]) -> dict[str, Any]:
    compacted = {
        "recorded_at": record.get("recorded_at"),
        "round_id": record.get("round_id"),
        "task_class": record.get("task_class"),
        "task_id": record.get("task_id"),
        "selection_mode": record.get("selection_mode"),
        "pairwise_outcome": deepcopy(record.get("pairwise_outcome", {})),
        "runs": [compact_benchmark_run(run) for run in record.get("runs", [])],
    }
    grade_schema = record.get("grade_schema") or record.get("score_schema")
    if grade_schema:
        compacted["grade_schema"] = grade_schema
    return compacted


def build_record_from_grade(
    *,
    round_payload: dict[str, Any],
    roster: dict[str, Any],
    rubric: dict[str, Any],
    task_id: str,
    winner: str,
    basis: str,
    alpha_note: str | None,
    bravo_note: str | None,
    shared_note: str | None,
) -> dict[str, Any]:
    if round_payload.get("status") != "completed":
        raise ValueError(f"round {round_payload['round_id']} is not completed yet")
    blocked_runs = []
    for run in round_payload.get("runs", []):
        classification = _classification_for_run(run)
        if classification["grade_blocked"]:
            blocked_runs.append(
                f"{public_reviewer_label(run['slot'])}: {classification['grade_block_reason'] or classification['review_status'] or 'unknown'}"
            )
    if blocked_runs:
        raise ValueError(
            "round contains interrupted or incomplete reviewers and should not be graded against model quality: "
            + "; ".join(blocked_runs)
        )
    basis = normalize_grade_basis(basis, rubric)
    indexed = variant_index(roster)
    runs_by_slot = {run["slot"]: run for run in round_payload["runs"]}
    alpha = deepcopy(runs_by_slot["alpha"])
    bravo = deepcopy(runs_by_slot["bravo"])
    alpha_variant = indexed[alpha["variant_id"]]
    bravo_variant = indexed[bravo["variant_id"]]
    alpha["grader_notes"] = alpha_note or shared_note or "alpha"
    bravo["grader_notes"] = bravo_note or shared_note or "bravo"
    alpha["cost_usd"] = compute_cost_usd(alpha_variant, alpha.get("usage", {}))
    bravo["cost_usd"] = compute_cost_usd(bravo_variant, bravo.get("usage", {}))
    if winner not in {"alpha", "bravo", "tie", alpha["variant_id"], bravo["variant_id"]}:
        raise ValueError("winner must be alpha, bravo, tie, or a concrete variant id")
    if winner == "alpha":
        winner_variant_id = alpha["variant_id"]
    elif winner == "bravo":
        winner_variant_id = bravo["variant_id"]
    else:
        winner_variant_id = winner
    if winner_variant_id == "tie" and not basis.startswith("tie_"):
        raise ValueError("tie winner requires tie_clean or tie_both_useful basis")
    if winner_variant_id != "tie" and basis.startswith("tie_"):
        raise ValueError("non-tie winner requires a non-tie basis")
    recorded_at = utc_now_iso()
    return compact_benchmark_record(
        {
            "recorded_at": recorded_at,
            "round_id": round_payload["round_id"],
            "task_class": round_payload["task_class"],
            "task_id": task_id,
            "selection_mode": round_payload["selection_mode"],
            "pairwise_outcome": {"winner": winner_variant_id, "basis": basis},
            "grade_schema": GRADE_SCHEMA,
            "runs": [alpha, bravo],
        }
    )


def expected_score(rating_a: float, rating_b: float) -> float:
    return 1.0 / (1.0 + math.pow(10.0, (rating_b - rating_a) / 400.0))


def update_elo(current_a: float, current_b: float, result: str, k_factor: float) -> tuple[float, float]:
    if result == "a":
        score_a, score_b = 1.0, 0.0
    elif result == "b":
        score_a, score_b = 0.0, 1.0
    else:
        score_a = score_b = 0.5
    expected_a = expected_score(current_a, current_b)
    expected_b = expected_score(current_b, current_a)
    return (
        current_a + k_factor * (score_a - expected_a),
        current_b + k_factor * (score_b - expected_b),
    )


def record_grade_basis(record: dict[str, Any]) -> str:
    basis = str((record.get("pairwise_outcome") or {}).get("basis") or "").strip()
    if basis:
        return basis
    if record.get("score_schema") or any(run.get("scores") for run in list(record.get("runs") or [])):
        return LEGACY_GRADE_BASIS
    return LEGACY_GRADE_BASIS


def percentage(numerator: int | float, denominator: int | float) -> float | None:
    if float(denominator) <= 0.0:
        return None
    return round((float(numerator) / float(denominator)) * 100.0, 3)


def aggregate_records(roster: dict[str, Any], records: list[dict[str, Any]], operational_state: dict[str, Any]) -> dict[str, Any]:
    settings = roster.get("settings", {})
    k_factor = float(settings.get("elo_k_factor", 24))
    summary: dict[str, Any] = {
        "generated_at": utc_now_iso(),
        "report_settings": _report_settings_snapshot(settings),
        "task_classes": {},
    }
    for task_class in TASK_CLASSES:
        active = eligible_variants(roster, task_class)
        ratings = {variant["id"]: 1500.0 for variant in active}
        metrics: dict[str, dict[str, Any]] = {
            variant["id"]: {
                "sample_count": 0,
                "win_count": 0,
                "tie_count": 0,
                "loss_count": 0,
                "finding_opportunity_count": 0,
                "valid_finding_count": 0,
                "missed_bug_loss_count": 0,
                "low_quality_loss_count": 0,
                "elapsed_values": [],
                "cost_values": [],
                "total_token_values": [],
            }
            for variant in active
        }
        recent_runs: list[dict[str, Any]] = []
        for record in (row for row in records if row.get("task_class") == task_class):
            record_runs = record.get("runs", [])
            if len(record_runs) != 2:
                continue
            left = record_runs[0]["variant_id"]
            right = record_runs[1]["variant_id"]
            winner = record.get("pairwise_outcome", {}).get("winner")
            basis = record_grade_basis(record)
            is_graded_record = winner in {left, right, "tie"}
            if is_graded_record and left in ratings and right in ratings:
                result = "tie" if winner == "tie" else ("a" if winner == left else "b")
                ratings[left], ratings[right] = update_elo(ratings[left], ratings[right], result, k_factor)
            for run in record_runs:
                variant_id = run["variant_id"]
                if variant_id not in metrics:
                    continue
                bucket = metrics[variant_id]
                usage = run.get("usage", {})
                cost_usd = run.get("cost_usd")
                if cost_usd is None:
                    variant = variant_index(roster)[variant_id]
                    cost_usd = compute_cost_usd(variant, usage)
                bucket["sample_count"] += 1
                if not is_graded_record:
                    continue
                if winner == "tie":
                    bucket["tie_count"] += 1
                    outcome = "tie"
                elif winner == variant_id:
                    bucket["win_count"] += 1
                    outcome = "win"
                else:
                    bucket["loss_count"] += 1
                    outcome = "loss"
                if basis in BUG_OPPORTUNITY_BASES:
                    bucket["finding_opportunity_count"] += 1
                if (outcome == "win" and basis in VALID_FINDING_WIN_BASES) or basis in BOTH_REVIEWERS_FOUND_BASES:
                    bucket["valid_finding_count"] += 1
                if outcome == "loss" and basis in MISSED_BUG_LOSS_BASES:
                    bucket["missed_bug_loss_count"] += 1
                if outcome == "loss" and basis in LOW_QUALITY_LOSS_BASES:
                    bucket["low_quality_loss_count"] += 1
                elapsed_seconds = run.get("elapsed_seconds")
                if isinstance(elapsed_seconds, (int, float)):
                    bucket["elapsed_values"].append(float(elapsed_seconds))
                total_tokens = total_usage_tokens(usage)
                if total_tokens > 0:
                    bucket["total_token_values"].append(total_tokens)
                if cost_usd is not None:
                    bucket["cost_values"].append(float(cost_usd))
                recent_runs.append(
                    {
                        "recorded_at": record.get("recorded_at"),
                        "task_id": record.get("task_id"),
                        "selection_mode": record.get("selection_mode"),
                        "variant_id": variant_id,
                        "winner": winner,
                        "basis": basis,
                        "elapsed_seconds": elapsed_seconds,
                    }
                )
        leaderboard = []
        for variant_id, bucket in metrics.items():
            sample_count = bucket["sample_count"]
            leaderboard.append(
                {
                    "variant_id": variant_id,
                    "variant_label": variant_id,
                    "sample_count": sample_count,
                    "elo": round(ratings[variant_id], 2),
                    "win_count": bucket["win_count"],
                    "tie_count": bucket["tie_count"],
                    "loss_count": bucket["loss_count"],
                    "wtl": f"{bucket['win_count']}/{bucket['tie_count']}/{bucket['loss_count']}",
                    "finding_opportunity_count": bucket["finding_opportunity_count"],
                    "valid_finding_count": bucket["valid_finding_count"],
                    "valid_finding_rate": percentage(bucket["valid_finding_count"], bucket["finding_opportunity_count"]),
                    "missed_bug_loss_count": bucket["missed_bug_loss_count"],
                    "missed_bug_loss_rate": percentage(bucket["missed_bug_loss_count"], bucket["finding_opportunity_count"]),
                    "low_quality_loss_count": bucket["low_quality_loss_count"],
                    "low_quality_loss_rate": percentage(bucket["low_quality_loss_count"], sample_count),
                    "median_elapsed_seconds": round(statistics.median(bucket["elapsed_values"]), 3) if bucket["elapsed_values"] else None,
                    "median_total_tokens": round(statistics.median(bucket["total_token_values"]), 1) if bucket["total_token_values"] else None,
                    "median_cost_usd": round(statistics.median(bucket["cost_values"]), 6) if bucket["cost_values"] else None,
                }
            )
        leaderboard.sort(key=lambda row: (row["elo"], row["valid_finding_rate"] or 0.0, row["sample_count"]), reverse=True)
        summary["task_classes"][task_class] = {
            "operational": operational_state["task_classes"][task_class],
            "leaderboard": leaderboard,
            "recent_runs": recent_runs[-50:],
        }
    return summary


def write_reports(state_dir: Path, summary: dict[str, Any]) -> None:
    write_json(state_dir / SUMMARY_FILENAME, summary)
    report_settings = dict(summary.get("report_settings") or {})
    k_factor = float(report_settings.get("elo_k_factor", 24.0))
    champion_min_samples = int(report_settings.get("promotion_min_samples", 20))
    champion_min_elo = float(report_settings.get("promotion_min_elo", 1550.0))
    champion_group_window = float(report_settings.get("promotion_champion_group_window", 25.0))
    lines = ["# Review Arena Leaderboard", ""]
    for task_class in TASK_CLASSES:
        task = summary["task_classes"][task_class]
        op = task["operational"]
        champion_ids = list(op.get("champion_variant_ids") or [])
        lines.append(f"## {public_task_name(task_class)}")
        lines.append("")
        lines.append(f"- Champion: `{', '.join(champion_ids) if champion_ids else 'none'}`")
        cooldowns = op.get("cooldowns") or {}
        if cooldowns:
            joined = "; ".join(
                f"`{variant_id}` until `{format_cooldown_until_for_display(entry.get('until'))}` (failures={entry.get('failure_count', 1)})"
                for variant_id, entry in sorted(cooldowns.items())
            )
            lines.append(f"- Cooldowns: {joined}")
        lines.append("")
        lines.append("| model | elo | samples | W/T/L | found/opp | found % | missed % | low-quality % | sec | tok/job | cost/job |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for row in task["leaderboard"]:
            lines.append(
                f"| {row['variant_label']} | {format_decimal(row['elo'])} | {row['sample_count']} | {row['wtl']} | "
                f"{row['valid_finding_count']}/{row['finding_opportunity_count']} | {format_decimal(row['valid_finding_rate'])} | "
                f"{format_decimal(row['missed_bug_loss_rate'])} | {format_decimal(row['low_quality_loss_rate'])} | {format_decimal(row['median_elapsed_seconds'])} | "
                f"{format_compact_tokens(row['median_total_tokens'])} | {format_cost_cents(row['median_cost_usd'])} |"
            )
        lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append(
        f"- Champion gate: `sample_count >= {champion_min_samples}` and `elo >= {format_decimal(champion_min_elo)}`. Champion group membership is `<= {format_decimal(champion_group_window)}` Elo behind the top eligible model."
    )
    lines.append(
        "- `found %` is valid findings over bug-present opportunities, including both reviewers on `tie_both_useful` and both-found coverage wins. `missed %` is missed-bug losses over bug-present opportunities. `low-quality %` is validity, false-positive, hallucinated, or fringe-finding losses over all samples."
    )
    lines.append("")
    lines.append("## match history")
    lines.append("")
    lines.append("| review | repo | model alpha | elo alpha | delta alpha | delta beta | elo beta | model beta |")
    lines.append("|---|---|---|---:|---:|---:|---:|---|")
    for row in recent_match_history(state_dir, k_factor=k_factor, limit=10):
        lines.append(
            f"| {row['review']} | {row['repo']} | {format_match_model(row['model_alpha'], row['outcome_alpha'])} | {format_decimal(row['elo_alpha'])} | "
            f"{format_signed_decimal(row['delta_alpha'])} | {format_signed_decimal(row['delta_beta'])} | "
            f"{format_decimal(row['elo_beta'])} | {format_match_model(row['model_beta'], row['outcome_beta'])} |"
        )
    lines.append("")
    _atomic_write_text(state_dir / "leaderboard.md", "\n".join(lines) + "\n")


def promote(roster: dict[str, Any], summary: dict[str, Any], operational_state: dict[str, Any]) -> dict[str, Any]:
    settings = roster["settings"]
    min_samples = int(settings["promotion_min_samples"])
    min_elo = float(settings.get("promotion_min_elo", 1550.0))
    champion_group_window = float(settings.get("promotion_champion_group_window", settings.get("promotion_min_elo_lead", 25.0)))
    state = deepcopy(operational_state)
    state["generated_at"] = utc_now_iso()
    for task_class in TASK_CLASSES:
        leaderboard = summary["task_classes"][task_class]["leaderboard"]
        slot = state["task_classes"][task_class]
        slot["champion_variant_id"] = None
        slot["champion_variant_ids"] = []
        slot["probation_variant_ids"] = []
        if not leaderboard:
            continue
        eligible = [
            row
            for row in leaderboard
            if row["sample_count"] >= min_samples
            and float(row["elo"]) >= min_elo
        ]
        champion_id_set: set[str] = set()
        if not eligible:
            slot["probation_variant_ids"] = _probation_variant_ids(
                leaderboard=leaderboard,
                champion_ids=champion_id_set,
                settings=settings,
            )
            continue
        top_eligible = eligible[0]
        champion_group = [
            row
            for row in eligible
            if (float(top_eligible["elo"]) - float(row["elo"])) <= champion_group_window
        ]
        champion_ids = [row["variant_id"] for row in champion_group]
        champion_id_set = set(champion_ids)
        slot["champion_variant_id"] = top_eligible["variant_id"]
        slot["champion_variant_ids"] = champion_ids
        slot["probation_variant_ids"] = _probation_variant_ids(
            leaderboard=leaderboard,
            champion_ids=champion_id_set,
            settings=settings,
        )
    return state
