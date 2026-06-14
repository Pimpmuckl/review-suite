from __future__ import annotations

import json
import os
import re
import subprocess
import time
from contextlib import contextmanager
from hashlib import blake2s
from pathlib import Path
from typing import Any

from .paths import normalize_cwd, utc_now_iso

WORKFLOW_SCHEMA_VERSION = 1
ANCHOR_HISTORY_LIMIT = 20
FOLLOWUP_MAX_COMMITS = 5
COHERENCE_MAX_FILES = 6
COHERENCE_MAX_LINES = 600
TOP_PATH_LIMIT = 3
BRANCH_PRESSURE_MAX_COMMITS = 25
BRANCH_PRESSURE_MAX_RECORDED_ANCHORS = 12
BRANCH_PRESSURE_MAX_FOLLOWUP_ANCHORS = 5
BRANCH_PRESSURE_MAX_FULL_REVIEW_ANCHORS = 4
BRANCH_PRESSURE_MAX_SIGNOFF_ANCHORS = 6
FOLLOWUP_CYCLE_LIMIT = 2
SAME_TIER_REVIEW_CAUTION_THRESHOLD = 6
SAME_TIER_REVIEW_HIGH_PRESSURE_THRESHOLD = 10
GATE_TASK_TO_LANE = {
    "phase_gate": "review_t2",
    "pr_gate": "review_t4",
}
LANE_STAGE_RANK = {
    "review_t1": 1,
    "review_t2": 2,
    "review_t3": 3,
    "review_t4": 4,
}
EFFECTIVE_BASE_METADATA_KEYS = (
    "base_upstream",
    "base_upstream_head",
    "base_upstream_unresolved",
    "requested_base_head",
    "effective_base_head",
    "base_ref_stale",
    "base_ref_relation",
)


def _git_text(review_cwd: Path, args: list[str], *, default_error: str) -> str:
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


def current_head(review_cwd: Path) -> str:
    return _git_text(review_cwd, ["git", "rev-parse", "HEAD"], default_error="git rev-parse HEAD failed").strip()


def current_branch(review_cwd: Path) -> str | None:
    branch = _git_text(
        review_cwd,
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        default_error="git rev-parse --abbrev-ref HEAD failed",
    ).strip()
    if not branch or branch == "HEAD":
        return None
    return branch


def resolve_ref(review_cwd: Path, ref: str) -> str:
    return _git_text(review_cwd, ["git", "rev-parse", ref], default_error=f"git rev-parse {ref} failed").strip()


def _optional_git_text(review_cwd: Path, args: list[str]) -> str | None:
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
        return None
    value = proc.stdout.strip()
    return value or None


def upstream_ref(review_cwd: Path, ref: str) -> str | None:
    base = str(ref or "").strip()
    if not base:
        return None
    upstream = _optional_git_text(
        review_cwd,
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", f"{base}@{{upstream}}"],
    )
    if upstream:
        return upstream
    remote = _optional_git_text(review_cwd, ["git", "config", f"branch.{base}.remote"])
    merge_ref = _optional_git_text(review_cwd, ["git", "config", f"branch.{base}.merge"])
    if not remote or not merge_ref:
        return None
    branch_name = merge_ref.removeprefix("refs/heads/").strip()
    if not branch_name:
        return None
    if remote == ".":
        return branch_name
    return f"{remote}/{branch_name}"


def effective_base_ref(review_cwd: Path, base: str) -> dict[str, Any]:
    requested = str(base or "").strip()
    if not requested:
        raise ValueError("base is required")
    payload: dict[str, Any] = {
        "base": requested,
        "requested_base": requested,
    }
    upstream = upstream_ref(review_cwd, requested)
    if not upstream or upstream == requested:
        return payload
    payload["base_upstream"] = upstream
    try:
        requested_head = resolve_ref(review_cwd, requested)
        upstream_head = resolve_ref(review_cwd, upstream)
    except ValueError:
        payload["base_upstream_unresolved"] = True
        return payload
    payload["requested_base_head"] = requested_head
    payload["base_upstream_head"] = upstream_head
    payload["base_ref_stale"] = requested_head != upstream_head
    if requested_head == upstream_head:
        payload["effective_base_head"] = requested_head
        payload["base_ref_relation"] = "same"
        return payload
    if is_ancestor(review_cwd, requested_head, upstream_head):
        payload["base"] = upstream
        payload["effective_base_head"] = upstream_head
        payload["base_ref_relation"] = "behind"
    elif is_ancestor(review_cwd, upstream_head, requested_head):
        payload["effective_base_head"] = requested_head
        payload["base_ref_relation"] = "ahead"
    else:
        payload["effective_base_head"] = requested_head
        payload["base_ref_relation"] = "diverged"
    return payload


def merge_base(review_cwd: Path, left_ref: str, right_ref: str = "HEAD") -> str:
    return _git_text(
        review_cwd,
        ["git", "merge-base", left_ref, right_ref],
        default_error=f"git merge-base {left_ref} {right_ref} failed",
    ).strip()


def is_ancestor(review_cwd: Path, ancestor_ref: str, descendant_ref: str = "HEAD") -> bool:
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor_ref, descendant_ref],
        cwd=str(review_cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    raise ValueError((proc.stderr or proc.stdout or "").strip() or f"git merge-base --is-ancestor {ancestor_ref} {descendant_ref} failed")


def validated_linear_review_range(
    review_cwd: Path,
    start_ref: str,
    end_ref: str,
    *,
    label: str = "native range review",
) -> dict[str, str]:
    start = str(start_ref or "").strip()
    end = str(end_ref or "").strip()
    if not start or not end:
        raise ValueError(f"{label} requires non-empty range start and end refs")
    resolved_start = resolve_ref(review_cwd, start)
    resolved_end = resolve_ref(review_cwd, end)
    head = current_head(review_cwd)
    if head != resolved_end:
        raise ValueError(
            f"{label} requires HEAD to match the range end. "
            "Check out the range end and review with --base <range-start>."
        )
    if not is_ancestor(review_cwd, resolved_start, resolved_end):
        raise ValueError(
            f"{label} requires the range start to be an ancestor of the range end. "
            "Review non-linear ranges as smaller single-commit slices."
        )
    return {
        "start": start,
        "end": end,
        "resolved_start": resolved_start,
        "resolved_end": resolved_end,
        "head": head,
    }


def has_committed_diff(review_cwd: Path, start_ref: str, end_ref: str = "HEAD") -> bool:
    proc = subprocess.run(
        ["git", "diff", "--quiet", f"{start_ref}..{end_ref}"],
        cwd=str(review_cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode == 0:
        return False
    if proc.returncode == 1:
        return True
    raise ValueError((proc.stderr or proc.stdout or "").strip() or f"git diff --quiet {start_ref}..{end_ref} failed")


def resolve_reviewed_head(review_cwd: Path, review_scope: dict[str, Any] | None = None) -> str:
    scope = dict(review_scope or {})
    scoped_head = str(scope.get("reviewed_head") or "").strip()
    if scoped_head:
        return resolve_ref(review_cwd, scoped_head)
    commit_end = str(scope.get("commit_end") or "").strip()
    if commit_end:
        return resolve_ref(review_cwd, commit_end)
    commit = str(scope.get("commit") or "").strip()
    if commit:
        return resolve_ref(review_cwd, commit)
    return current_head(review_cwd)


def anchor_updates_branch_state(
    *,
    lane: str,
    review_scope: dict[str, Any] | None,
    reviewed_head: str,
    current_head: str,
) -> bool:
    scope = dict(review_scope or {})
    if lane in {"review-followup", "review-github"}:
        return reviewed_head == current_head
    if scope.get("commit") or scope.get("commit_end"):
        return False
    if lane in {"review_t1", "review_t2", "review_t3", "review_t4"}:
        return reviewed_head == current_head
    if scope.get("base"):
        return reviewed_head == current_head
    return False


def diff_artifact(review_cwd: Path, start_ref: str, end_ref: str = "HEAD") -> str:
    return _git_text(
        review_cwd,
        ["git", "diff", "--find-renames", "--stat", "--patch", f"{start_ref}..{end_ref}"],
        default_error=f"git diff {start_ref}..{end_ref} failed",
    )


def worktree_status_entries(review_cwd: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    fields = _git_text(
        review_cwd,
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        default_error="git status --porcelain failed",
    ).split("\0")
    idx = 0
    while idx < len(fields):
        raw_line = fields[idx]
        idx += 1
        if not raw_line:
            continue
        code = raw_line[:2]
        path = raw_line[3:] if len(raw_line) > 3 else ""
        if code.strip().startswith("R") or code.strip().startswith("C"):
            idx += 1
        rows.append({"code": code, "path": path})
    return rows


def _is_review_suite_scratch_path(path_text: str) -> bool:
    normalized = path_text.replace("\\", "/").strip().strip('"')
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized == ".review-suite" or normalized.startswith(".review-suite/")


def meaningful_worktree_status_entries(review_cwd: Path) -> list[dict[str, str]]:
    return [
        item
        for item in worktree_status_entries(review_cwd)
        if not _is_review_suite_scratch_path(str(item.get("path") or ""))
    ]


def has_worktree_changes(review_cwd: Path) -> bool:
    return bool(meaningful_worktree_status_entries(review_cwd))


def branch_diff_paths(review_cwd: Path, base: str, merge_base_ref: str | None = None) -> set[str]:
    resolved_merge_base = str(merge_base_ref or "").strip() or merge_base(review_cwd, base, "HEAD")
    return diff_paths_between(review_cwd, resolved_merge_base, "HEAD")


def diff_paths_between(review_cwd: Path, left_ref: str, right_ref: str) -> set[str]:
    return {
        line.strip()
        for line in _git_text(
            review_cwd,
            ["git", "diff", "--name-only", "--find-renames", f"{left_ref}..{right_ref}"],
            default_error=f"git diff --name-only {left_ref}..{right_ref} failed",
        ).splitlines()
        if line.strip()
    }


def diff_patch_between(review_cwd: Path, left_ref: str, right_ref: str) -> str:
    return _git_text(
        review_cwd,
        ["git", "diff", "--find-renames", "--patch", f"{left_ref}..{right_ref}"],
        default_error=f"git diff --patch {left_ref}..{right_ref} failed",
    )


def merge_base_drift_scope(
    *,
    review_cwd: Path,
    recorded_merge_base: str,
    current_merge_base: str,
    reviewed_head: str,
    current_head: str = "HEAD",
) -> dict[str, Any]:
    base_changed_paths = diff_paths_between(review_cwd, recorded_merge_base, current_merge_base)
    recorded_branch_paths = diff_paths_between(review_cwd, recorded_merge_base, reviewed_head)
    current_branch_paths = diff_paths_between(review_cwd, current_merge_base, current_head)
    branch_paths = recorded_branch_paths | current_branch_paths
    overlapping_paths = sorted(base_changed_paths & branch_paths)
    return {
        "base_changed_paths": sorted(base_changed_paths),
        "recorded_branch_paths": sorted(recorded_branch_paths),
        "current_branch_paths": sorted(current_branch_paths),
        "overlapping_paths": overlapping_paths,
        "patch_equivalent": diff_patch_between(review_cwd, recorded_merge_base, reviewed_head)
        == diff_patch_between(review_cwd, current_merge_base, current_head),
    }


def dirty_worktree_scope(review_cwd: Path, base: str, merge_base_ref: str | None = None) -> dict[str, Any]:
    dirty_paths = sorted(
        {
            str(item.get("path") or "").strip()
            for item in meaningful_worktree_status_entries(review_cwd)
            if str(item.get("path") or "").strip()
        }
    )
    if not dirty_paths:
        return {
            "dirty_paths": [],
            "related_dirty_paths": [],
            "unrelated_dirty_paths": [],
            "all_dirty_paths_outside_branch_diff": False,
        }
    committed_branch_paths = branch_diff_paths(review_cwd, base, merge_base_ref=merge_base_ref)
    related_dirty_paths = [path for path in dirty_paths if path in committed_branch_paths]
    unrelated_dirty_paths = [path for path in dirty_paths if path not in committed_branch_paths]
    return {
        "dirty_paths": dirty_paths,
        "related_dirty_paths": related_dirty_paths,
        "unrelated_dirty_paths": unrelated_dirty_paths,
        "all_dirty_paths_outside_branch_diff": bool(dirty_paths) and not related_dirty_paths,
    }


def worktree_diff_artifact(review_cwd: Path, anchor_ref: str = "HEAD") -> str:
    tracked_diff = _git_text(
        review_cwd,
        ["git", "diff", "--find-renames", "--stat", "--patch", anchor_ref],
        default_error=f"git diff {anchor_ref} failed",
    )
    untracked_paths = [
        str(item.get("path") or "").strip()
        for item in meaningful_worktree_status_entries(review_cwd)
        if str(item.get("code") or "") == "??" and str(item.get("path") or "").strip()
    ]
    if not untracked_paths:
        return tracked_diff
    prefix = tracked_diff.rstrip()
    if prefix:
        prefix += "\n\n"
    return prefix + "=== UNTRACKED FILES (content not embedded) ===\n" + "\n".join(untracked_paths) + "\n"


def _slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    slug = slug.strip("-._")
    return slug or "item"


def _short_sha(value: str) -> str:
    return str(value or "").strip()[:12]


def repo_token(review_cwd: Path) -> str:
    normalized = normalize_cwd(str(review_cwd))
    label = _slugify(Path(normalized).name or "repo")
    digest = blake2s(normalized.encode("utf-8"), digest_size=6).hexdigest()
    return f"{label}-{digest}"


def branch_token(branch: str | None, head: str) -> str:
    if branch:
        label = _slugify(branch)
        digest = blake2s(branch.encode("utf-8"), digest_size=4).hexdigest()
        return f"{label}-{digest}"
    return f"detached-{_short_sha(head)}"


def workflow_state_path(
    *,
    state_dir: Path,
    review_cwd: Path,
    branch: str | None = None,
    head: str | None = None,
) -> Path:
    resolved_head = head or current_head(review_cwd)
    resolved_branch = current_branch(review_cwd) if branch is None else branch
    return state_dir / "workflow" / repo_token(review_cwd) / f"{branch_token(resolved_branch, resolved_head)}.json"


def _workflow_repo_dir(*, state_dir: Path, review_cwd: Path) -> Path:
    return state_dir / "workflow" / repo_token(review_cwd)


def _detached_fallback_state(*, state_dir: Path, review_cwd: Path, head: str) -> dict[str, Any] | None:
    repo_dir = _workflow_repo_dir(state_dir=state_dir, review_cwd=review_cwd)
    if not repo_dir.exists():
        return None
    best_state: dict[str, Any] | None = None
    best_distance: int | None = None
    for candidate in [repo_dir / "detached.json", *sorted(repo_dir.glob("detached-*.json"))]:
        if not candidate.exists():
            continue
        try:
            state = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
        candidate_head = str(state.get("current_head") or "").strip()
        if not candidate_head:
            continue
        try:
            resolved_candidate_head = resolve_ref(review_cwd, candidate_head)
        except ValueError:
            continue
        if not is_ancestor(review_cwd, resolved_candidate_head, head):
            continue
        distance = commit_distance(review_cwd, resolved_candidate_head, head)
        if best_distance is None or distance < best_distance:
            best_state = state
            best_distance = distance
    return best_state


@contextmanager
def workflow_state_lock(
    *,
    state_dir: Path,
    review_cwd: Path,
    branch: str | None = None,
    head: str | None = None,
    timeout_seconds: int = 30,
    poll_seconds: float = 0.1,
):
    resolved_branch = current_branch(review_cwd) if branch is None else branch
    resolved_head = head or current_head(review_cwd)
    lock_name = f"workflow-{repo_token(review_cwd)}-{branch_token(resolved_branch, resolved_head)}"
    locks_dir = state_dir / ".locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    lock_path = locks_dir / f"{lock_name}.lock"
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
        except FileNotFoundError:
            pass


def load_workflow_state(
    *,
    state_dir: Path,
    review_cwd: Path,
    branch: str | None = None,
    head: str | None = None,
) -> dict[str, Any] | None:
    resolved_head = head or current_head(review_cwd)
    resolved_branch = current_branch(review_cwd) if branch is None else branch
    path = workflow_state_path(state_dir=state_dir, review_cwd=review_cwd, branch=resolved_branch, head=resolved_head)
    if not path.exists():
        if resolved_branch is None:
            return _detached_fallback_state(state_dir=state_dir, review_cwd=review_cwd, head=resolved_head)
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
    except OSError:
        return []
    return rows


def _gate_signoff_decisions_by_round(state_dir: Path) -> dict[str, dict[str, Any]]:
    decisions: dict[str, dict[str, Any]] = {}
    for decision in _read_jsonl(state_dir / "gate_signoffs.jsonl"):
        round_id = str(decision.get("round_id") or "").strip()
        if round_id:
            decisions[round_id] = dict(decision)
    return decisions


def _gate_record_cwd(record: dict[str, Any]) -> str:
    value = str(record.get("review_cwd_normalized") or record.get("review_cwd") or "").strip()
    if not value:
        return ""
    try:
        return normalize_cwd(value)
    except Exception:
        return value


def _gate_record_reviewed_head(record: dict[str, Any]) -> str:
    scope = dict(record.get("review_scope") or {})
    return str(scope.get("reviewed_head") or scope.get("commit_end") or scope.get("commit") or "").strip()


def _gate_record_base(record: dict[str, Any]) -> str:
    scope = dict(record.get("review_scope") or {})
    return str(scope.get("base") or "").strip()


def _gate_record_has_blocked_runs(record: dict[str, Any]) -> bool:
    runs = [run for run in list(record.get("runs") or []) if isinstance(run, dict)]
    return bool(runs) and any(bool(run.get("grade_blocked")) for run in runs)


def _gate_record_advances_stage(record: dict[str, Any], decision: dict[str, Any] | None = None) -> bool:
    runs = [run for run in list(record.get("runs") or []) if isinstance(run, dict)]
    if not runs or _gate_record_has_blocked_runs(record):
        return False
    verdict = str((decision or {}).get("verdict") or "").strip()
    if verdict in {"clean", "findings"}:
        return True
    if str(record.get("signoff_status") or "").strip() == "pending":
        return True
    return all(str(run.get("review_status") or "").strip() == "completed" for run in runs)


def _latest_current_head_followup_after(
    *,
    state: dict[str, Any] | None,
    review_cwd: Path,
    head: str,
    after: str,
    source_gate_round_id: str | None = None,
    source_reviewed_head: str | None = None,
) -> dict[str, Any] | None:
    if not state:
        return None
    source_gate_round_id = str(source_gate_round_id or "").strip()
    source_reviewed_head = str(source_reviewed_head or "").strip()
    candidates: list[dict[str, Any]] = []
    for anchor in [item for item in list(state.get("anchors") or []) if isinstance(item, dict)]:
        if str(anchor.get("lane") or "") != "review-followup":
            continue
        recorded_at = str(anchor.get("recorded_at") or "").strip()
        if after and recorded_at and recorded_at <= after:
            continue
        scope = dict(anchor.get("review_scope") or {})
        anchor_source_round_id = str(scope.get("source_gate_round_id") or "").strip()
        if source_gate_round_id and anchor_source_round_id and anchor_source_round_id != source_gate_round_id:
            continue
        if source_gate_round_id and not anchor_source_round_id and source_reviewed_head:
            source_head = str(scope.get("commit") or "").strip()
            if source_head:
                try:
                    if resolve_ref(review_cwd, source_head) != source_reviewed_head:
                        continue
                except ValueError:
                    continue
        reviewed_head = str(anchor.get("reviewed_head") or anchor.get("current_head_at_record") or "").strip()
        if not reviewed_head:
            continue
        try:
            if resolve_ref(review_cwd, reviewed_head) == head:
                candidates.append(dict(anchor))
        except ValueError:
            continue
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: str(item.get("recorded_at") or ""))[-1]


def _gate_record_order_key(record: dict[str, Any], decision: dict[str, Any] | None = None) -> str:
    decision = decision or {}
    return str(
        decision.get("recorded_at")
        or record.get("review_completed_at")
        or record.get("recorded_at")
        or record.get("round_id")
        or ""
    )


def latest_unresolved_gate_findings_candidate(
    *,
    state_dir: Path,
    state: dict[str, Any] | None,
    review_cwd: Path,
    base: str,
    branch: str | None,
    head: str,
    current_stage_lane: str | None,
) -> dict[str, Any] | None:
    if current_stage_lane not in {"review_t2", "review_t4"}:
        return None
    normalized_cwd = normalize_cwd(str(review_cwd))
    requested_base = str(base or "").strip()
    decisions = _gate_signoff_decisions_by_round(state_dir)
    findings: list[dict[str, Any]] = []
    clean_records: list[dict[str, Any]] = []
    for record in _read_jsonl(state_dir / "gate_runs.jsonl"):
        round_id = str(record.get("round_id") or "").strip()
        decision = decisions.get(round_id) or {}
        if not _gate_record_advances_stage(record, decision):
            continue
        task_class = str(record.get("task_class") or "")
        lane = GATE_TASK_TO_LANE.get(task_class)
        if lane != current_stage_lane:
            continue
        if _gate_record_cwd(record) != normalized_cwd:
            continue
        task_id = str(record.get("task_id") or "").strip()
        if branch and branch != "HEAD" and task_id and task_id != branch:
            continue
        if requested_base and _gate_record_base(record) != requested_base:
            continue
        verdict = str(decision.get("verdict") or "").strip()
        reviewed_head = _gate_record_reviewed_head(record)
        if not reviewed_head:
            continue
        try:
            resolved_head = resolve_ref(review_cwd, reviewed_head)
        except ValueError:
            continue
        if verdict == "clean" and resolved_head == head:
            clean_records.append(
                {
                    "round_id": round_id,
                    "reviewed_head": resolved_head,
                    "order_key": _gate_record_order_key(record, decision),
                }
            )
        if verdict != "findings":
            continue
        decision_at = str(decision.get("recorded_at") or "").strip()
        followup_anchor = _latest_current_head_followup_after(
            state=state,
            review_cwd=review_cwd,
            head=head,
            after=decision_at,
            source_gate_round_id=round_id,
            source_reviewed_head=resolved_head,
        )
        findings.append(
            {
                "lane": lane,
                "round_id": round_id,
                "reviewed_head": resolved_head,
                "reviewed_head_raw": reviewed_head,
                "recorded_at": str(record.get("review_completed_at") or record.get("recorded_at") or "").strip(),
                "decision_recorded_at": decision_at,
                "order_key": _gate_record_order_key(record, decision),
                "followup_anchor": followup_anchor,
            }
        )
    for finding in sorted(findings, key=lambda item: str(item.get("order_key") or ""), reverse=True):
        finding_order = str(finding.get("order_key") or "")
        if any(str(clean.get("order_key") or "") > finding_order for clean in clean_records):
            continue
        return finding
    return None


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


def compact_review_scope(review_scope: dict[str, Any] | None) -> dict[str, Any]:
    scope = dict(review_scope or {})
    allowed = (
        "base",
        "commit",
        "commit_end",
        "merge_base",
        "reviewed_head",
        "branch_base",
        "requested_base",
        *EFFECTIVE_BASE_METADATA_KEYS,
        "source_gate_lane",
        "source_gate_reviewed_head",
        "source_gate_round_id",
    )
    return {
        key: scope[key]
        for key in allowed
        if key in scope and scope[key] not in (None, "", [], {})
    }


def compact_workflow_anchor(anchor: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "recorded_at",
        "lane",
        "base",
        "reviewed_head",
        "current_head_at_record",
        "review_scope",
        "updates_branch_state",
        "round_id",
        "task_id",
    )
    compacted = {
        key: anchor[key]
        for key in allowed
        if key in anchor and anchor[key] not in (None, "", [], {})
    }
    compacted["review_scope"] = compact_review_scope(dict(anchor.get("review_scope") or {}))
    return compacted


def record_review_anchor(
    *,
    state_dir: Path,
    review_cwd: Path,
    lane: str,
    base: str | None = None,
    review_scope: dict[str, Any] | None = None,
    reviewed_head: str | None = None,
    round_id: str | None = None,
    task_id: str | None = None,
    output_refs: list[str] | None = None,
    session_id: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    branch = current_branch(review_cwd)
    head = current_head(review_cwd)
    with workflow_state_lock(state_dir=state_dir, review_cwd=review_cwd, branch=branch, head=head):
        path = workflow_state_path(state_dir=state_dir, review_cwd=review_cwd, branch=branch, head=head)
        existing = load_workflow_state(state_dir=state_dir, review_cwd=review_cwd, branch=branch, head=head) or {}
        resolved_reviewed_head = reviewed_head or resolve_reviewed_head(review_cwd, review_scope)
        updates_branch_state = anchor_updates_branch_state(
            lane=lane,
            review_scope=review_scope,
            reviewed_head=resolved_reviewed_head,
            current_head=head,
        )
        recorded_at = utc_now_iso()
        anchor = compact_workflow_anchor({
            "recorded_at": recorded_at,
            "lane": lane,
            "base": base,
            "reviewed_head": resolved_reviewed_head,
            "current_head_at_record": head,
            "review_scope": dict(review_scope or {}),
            "updates_branch_state": updates_branch_state,
        })
        if round_id:
            anchor["round_id"] = round_id
        if task_id:
            anchor["task_id"] = task_id
        anchors = [
            compact_workflow_anchor(item)
            for item in list(existing.get("anchors") or [])
            if isinstance(item, dict)
        ]
        anchors.append(anchor)
        anchors = anchors[-ANCHOR_HISTORY_LIMIT:]
        payload = {
            "schema_version": WORKFLOW_SCHEMA_VERSION,
            "repo_root": normalize_cwd(str(review_cwd)),
            "repo_token": repo_token(review_cwd),
            "branch": branch,
            "base": str(existing.get("base") or "") or base,
            "current_head": head,
            "anchors": anchors,
        }
        if updates_branch_state:
            payload["base"] = base
            payload["last_reviewed_head"] = resolved_reviewed_head
            payload["last_reviewed_lane"] = lane
            payload["last_reviewed_at"] = recorded_at
        else:
            for key in ("last_reviewed_head", "last_reviewed_lane", "last_reviewed_at"):
                existing_value = str(existing.get(key) or "").strip()
                if existing_value:
                    payload[key] = existing_value
        _atomic_write_json(path, payload)
        return payload


def _parse_numstat_row(line: str) -> dict[str, Any] | None:
    parts = line.rstrip().split("\t", 2)
    if len(parts) != 3:
        return None
    added_raw, deleted_raw, path = parts
    added = int(added_raw) if added_raw.isdigit() else 0
    deleted = int(deleted_raw) if deleted_raw.isdigit() else 0
    return {
        "path": path,
        "added": added,
        "deleted": deleted,
        "lines_changed": added + deleted,
    }


def diff_stats(review_cwd: Path, start_ref: str, end_ref: str = "HEAD") -> dict[str, Any]:
    commit_count_text = _git_text(
        review_cwd,
        ["git", "rev-list", "--count", f"{start_ref}..{end_ref}"],
        default_error=f"git rev-list --count {start_ref}..{end_ref} failed",
    ).strip()
    rows = [
        row
        for row in (
            _parse_numstat_row(line)
            for line in _git_text(
                review_cwd,
                ["git", "diff", "--numstat", "--find-renames", f"{start_ref}..{end_ref}"],
                default_error=f"git diff --numstat {start_ref}..{end_ref} failed",
            ).splitlines()
        )
        if row is not None
    ]
    top_paths = sorted(rows, key=lambda item: (-int(item["lines_changed"]), str(item["path"])))[:TOP_PATH_LIMIT]
    return {
        "commits_since_anchor": int(commit_count_text or 0),
        "files_changed": len(rows),
        "lines_changed": sum(int(row["lines_changed"]) for row in rows),
        "top_paths": top_paths,
    }


def worktree_diff_stats(review_cwd: Path, anchor_ref: str = "HEAD") -> dict[str, Any]:
    rows = [
        row
        for row in (
            _parse_numstat_row(line)
            for line in _git_text(
                review_cwd,
                ["git", "diff", "--numstat", "--find-renames", anchor_ref],
                default_error=f"git diff --numstat {anchor_ref} failed",
            ).splitlines()
        )
        if row is not None
    ]
    indexed_paths = {str(row["path"]) for row in rows}
    untracked_paths = [
        str(item.get("path") or "").strip()
        for item in meaningful_worktree_status_entries(review_cwd)
        if str(item.get("code") or "") == "??" and str(item.get("path") or "").strip()
    ]
    for path in untracked_paths:
        if path in indexed_paths:
            continue
        rows.append(
            {
                "path": path,
                "added": 0,
                "deleted": 0,
                "lines_changed": 0,
            }
        )
    top_paths = sorted(rows, key=lambda item: (-int(item["lines_changed"]), str(item["path"])))[:TOP_PATH_LIMIT]
    return {
        "commits_since_anchor": 0,
        "files_changed": len(rows),
        "lines_changed": sum(int(row["lines_changed"]) for row in rows),
        "top_paths": top_paths,
        "worktree_dirty": True,
        "untracked_files": len(untracked_paths),
    }


def classify_delta_recommendation(delta: dict[str, Any]) -> dict[str, str]:
    if int(delta["files_changed"]) > COHERENCE_MAX_FILES or int(delta["lines_changed"]) > COHERENCE_MAX_LINES:
        return {
            "recommendation": "coherence-review",
            "reason": "diff_churn_exceeded",
            "note": "The post-review delta is large enough that coherence/reset review is safer than another narrow follow-up.",
        }
    if int(delta["commits_since_anchor"]) > FOLLOWUP_MAX_COMMITS:
        return {
            "recommendation": "full-review",
            "reason": "commit_churn_exceeded",
            "note": "Too many commits landed after the last review anchor for a narrow interdiff follow-up.",
        }
    return {
        "recommendation": "review-followup",
        "reason": "small_delta_after_review",
        "note": "Use the interdiff follow-up lane against the last reviewed head.",
    }


def latest_anchor(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not state:
        return None
    anchors = [item for item in list(state.get("anchors") or []) if isinstance(item, dict)]
    if not anchors:
        return None
    return anchors[-1]


def latest_branch_anchor(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not state:
        return None
    anchors = [item for item in list(state.get("anchors") or []) if isinstance(item, dict)]
    for anchor in reversed(anchors):
        if "updates_branch_state" in anchor:
            if bool(anchor.get("updates_branch_state")):
                return anchor
            continue
        if anchor_updates_branch_state(
            lane=str(anchor.get("lane") or ""),
            review_scope=dict(anchor.get("review_scope") or {}),
            reviewed_head=str(anchor.get("reviewed_head") or ""),
            current_head=str(anchor.get("current_head_at_record") or anchor.get("reviewed_head") or ""),
        ):
            return anchor
    return None


def latest_full_review_anchor(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not state:
        return None
    anchors = [item for item in list(state.get("anchors") or []) if isinstance(item, dict)]
    for anchor in reversed(anchors):
        if str(anchor.get("lane") or "") in {"review_t1", "review_t3"}:
            return anchor
    return None


def latest_followup_pressure_checkpoint_anchor(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not state:
        return None
    anchors = [item for item in list(state.get("anchors") or []) if isinstance(item, dict)]
    for anchor in reversed(anchors):
        if str(anchor.get("lane") or "") in {"review_t1", "review_t2", "review_t3", "review_t4", "review-github"}:
            return anchor
    return None


def latest_base_review_context_anchor(
    state: dict[str, Any] | None,
    *,
    requested_base: str,
) -> dict[str, Any] | None:
    if not state:
        return None
    anchors = [item for item in list(state.get("anchors") or []) if isinstance(item, dict)]
    requested = str(requested_base or "").strip()
    fallback: dict[str, Any] | None = None
    for anchor in reversed(anchors):
        if str(anchor.get("lane") or "") not in {"review_t1", "review_t2", "review_t3", "review_t4"}:
            continue
        scope = dict(anchor.get("review_scope") or {})
        if scope.get("commit") or scope.get("commit_end"):
            continue
        if not str(scope.get("merge_base") or "").strip():
            continue
        anchor_base = str(scope.get("base") or anchor.get("base") or "").strip()
        if not anchor_base:
            continue
        anchor_requested_base = str(scope.get("requested_base") or "").strip()
        anchor_branch_base = str(scope.get("branch_base") or "").strip()
        if requested and requested in {anchor_base, anchor_requested_base, anchor_branch_base}:
            return anchor
        if fallback is None:
            fallback = anchor
    return fallback


def review_scope_matches_requested_base(
    *,
    anchor: dict[str, Any],
    review_scope: dict[str, Any],
    requested_base: str,
) -> bool:
    requested = str(requested_base or "").strip()
    if not requested:
        return False
    return requested in {
        str(review_scope.get("base") or "").strip(),
        str(review_scope.get("requested_base") or "").strip(),
        str(review_scope.get("branch_base") or "").strip(),
        str(anchor.get("base") or "").strip(),
    }


def current_stage_full_review_lane(
    state: dict[str, Any] | None,
    *,
    state_dir: Path | None = None,
    review_cwd: Path | None = None,
    branch: str | None = None,
) -> str | None:
    candidates: list[str] = []
    anchors = [item for item in list((state or {}).get("anchors") or []) if isinstance(item, dict)]
    for anchor in anchors:
        lane = str(anchor.get("lane") or "")
        if lane in LANE_STAGE_RANK:
            candidates.append(lane)
    if state_dir is not None and review_cwd is not None:
        normalized_cwd = normalize_cwd(str(review_cwd))
        decisions = _gate_signoff_decisions_by_round(state_dir)
        for record in _read_jsonl(state_dir / "gate_runs.jsonl"):
            round_id = str(record.get("round_id") or "").strip()
            if not _gate_record_advances_stage(record, decisions.get(round_id)):
                continue
            record_cwd = str(record.get("review_cwd_normalized") or record.get("review_cwd") or "").strip()
            if record_cwd:
                try:
                    record_cwd = normalize_cwd(record_cwd)
                except Exception:
                    pass
            if record_cwd != normalized_cwd:
                continue
            task_id = str(record.get("task_id") or "").strip()
            if branch and task_id and task_id != branch:
                continue
            lane = GATE_TASK_TO_LANE.get(str(record.get("task_class") or ""))
            if lane:
                candidates.append(lane)
    if not candidates:
        return None
    return max(candidates, key=lambda lane: LANE_STAGE_RANK[lane])


def same_tier_review_pressure(
    *,
    state: dict[str, Any] | None,
    state_dir: Path | None,
    review_cwd: Path,
    branch: str | None,
) -> dict[str, Any] | None:
    lane = current_stage_full_review_lane(state, state_dir=state_dir, review_cwd=review_cwd, branch=branch)
    if lane is None:
        return None
    count = 0
    seen_round_ids: set[str] = set()
    for anchor in [item for item in list((state or {}).get("anchors") or []) if isinstance(item, dict)]:
        if str(anchor.get("lane") or "") != lane:
            continue
        round_id = str(anchor.get("round_id") or "").strip()
        if round_id:
            seen_round_ids.add(round_id)
        count += 1
    if state_dir is not None:
        normalized_cwd = normalize_cwd(str(review_cwd))
        decisions = _gate_signoff_decisions_by_round(state_dir)
        for record in _read_jsonl(state_dir / "gate_runs.jsonl"):
            if GATE_TASK_TO_LANE.get(str(record.get("task_class") or "")) != lane:
                continue
            round_id = str(record.get("round_id") or "").strip()
            if not _gate_record_advances_stage(record, decisions.get(round_id)):
                continue
            record_cwd = str(record.get("review_cwd_normalized") or record.get("review_cwd") or "").strip()
            if record_cwd:
                try:
                    record_cwd = normalize_cwd(record_cwd)
                except Exception:
                    pass
            if record_cwd != normalized_cwd:
                continue
            task_id = str(record.get("task_id") or "").strip()
            if branch and task_id and task_id != branch:
                continue
            if round_id and round_id in seen_round_ids:
                continue
            if round_id:
                seen_round_ids.add(round_id)
            count += 1
    if count < SAME_TIER_REVIEW_CAUTION_THRESHOLD:
        return None
    status = "high_pressure" if count >= SAME_TIER_REVIEW_HIGH_PRESSURE_THRESHOLD else "caution"
    instruction = (
        "Before another same-tier review, check the full diff. If findings are not converging or product decisions are unclear, pause and discuss."
        if status == "high_pressure"
        else "Before another same-tier review, confirm the remaining findings are converging and patch-sized."
    )
    return {
        "status": status,
        "tier": lane,
        "same_tier_true_run_count": count,
        "caution_threshold": SAME_TIER_REVIEW_CAUTION_THRESHOLD,
        "high_pressure_threshold": SAME_TIER_REVIEW_HIGH_PRESSURE_THRESHOLD,
        "note": "This count only includes full tiered review runs, not review-followup anchors.",
        "instruction": instruction,
    }


def add_stage_full_review_lane(
    decision: dict[str, Any],
    *,
    state: dict[str, Any] | None,
    state_dir: Path | None = None,
    review_cwd: Path | None = None,
    branch: str | None = None,
) -> dict[str, Any]:
    if str(decision.get("recommendation") or "") not in {"coherence-review", "full-review"}:
        return decision
    lane = current_stage_full_review_lane(state, state_dir=state_dir, review_cwd=review_cwd, branch=branch)
    if lane is None:
        return decision
    enriched = dict(decision)
    enriched["recommended_lane"] = lane
    if str(decision.get("recommendation") or "") == "coherence-review":
        enriched["note"] = (
            f"Stop chaining narrow interdiff follow-ups on this branch. Run {lane} as the fresh full-diff lane "
            "for the current review stage before more follow-up. If that full-diff pass shows the branch is no longer coherent, split or checkpoint the next logical slice."
        )
    elif str(decision.get("recommendation") or "") == "full-review":
        enriched["note"] = f"Run {lane} as the fresh full-diff lane for the current review stage."
    return enriched


def commit_distance(review_cwd: Path, start_ref: str, end_ref: str = "HEAD") -> int:
    count_text = _git_text(
        review_cwd,
        ["git", "rev-list", "--count", f"{start_ref}..{end_ref}"],
        default_error=f"git rev-list --count {start_ref}..{end_ref} failed",
    ).strip()
    return int(count_text or 0)


def followup_cycle_pressure(*, state: dict[str, Any] | None) -> dict[str, Any] | None:
    checkpoint = latest_followup_pressure_checkpoint_anchor(state)
    if checkpoint is None:
        return None
    anchors = [item for item in list(state.get("anchors") or []) if isinstance(item, dict)]
    checkpoint_index = anchors.index(checkpoint)
    trailing = anchors[checkpoint_index + 1 :]
    followup_anchor_count = sum(1 for item in trailing if str(item.get("lane") or "") == "review-followup")
    if followup_anchor_count <= FOLLOWUP_CYCLE_LIMIT:
        return None
    signoff_anchor_count = sum(1 for item in trailing if str(item.get("lane") or "") in {"review_t2", "review_t4"})
    return {
        "last_full_review_lane": str(checkpoint.get("lane") or ""),
        "last_full_review_head": str(checkpoint.get("reviewed_head") or ""),
        "followup_anchor_count_since_full_review": followup_anchor_count,
        "signoff_anchor_count_since_full_review": signoff_anchor_count,
        "recommendation": "coherence-review",
        "reason": "followup_cycle_limit_exceeded",
        "note": (
            f"The branch already used {followup_anchor_count} follow-up rounds and {signoff_anchor_count} signoff rounds "
            f"since the last full checkpoint {str(checkpoint.get('lane') or '')} at {str(checkpoint.get('reviewed_head') or '')[:12]}. "
            "Stop chaining more narrow interdiff follow-ups on this branch. Run a fresh branch-wide correctness review or split the next logical slice into a new stacked PR."
        ),
    }


def branch_review_pressure(
    *,
    state: dict[str, Any] | None,
    review_cwd: Path,
    base: str,
    merge_base_ref: str | None = None,
) -> dict[str, Any] | None:
    if not state or not str(base or "").strip():
        return None
    anchors = [item for item in list(state.get("anchors") or []) if isinstance(item, dict)]
    if not anchors:
        return None
    resolved_merge_base = str(merge_base_ref or "").strip() or merge_base(review_cwd, str(base), "HEAD")
    commits_since_base = commit_distance(review_cwd, resolved_merge_base, "HEAD")
    recorded_anchor_count = len(anchors)
    followup_anchor_count = sum(1 for item in anchors if str(item.get("lane") or "") == "review-followup")
    full_review_anchor_count = sum(1 for item in anchors if str(item.get("lane") or "") in {"review_t1", "review_t3"})
    signoff_anchor_count = sum(1 for item in anchors if str(item.get("lane") or "") in {"review_t2", "review_t4"})
    if commits_since_base < BRANCH_PRESSURE_MAX_COMMITS:
        return None
    if recorded_anchor_count < BRANCH_PRESSURE_MAX_RECORDED_ANCHORS:
        return None
    if (
        followup_anchor_count < BRANCH_PRESSURE_MAX_FOLLOWUP_ANCHORS
        and full_review_anchor_count < BRANCH_PRESSURE_MAX_FULL_REVIEW_ANCHORS
        and signoff_anchor_count < BRANCH_PRESSURE_MAX_SIGNOFF_ANCHORS
    ):
        return None
    return {
        "branch_commits_since_base": commits_since_base,
        "recorded_review_anchor_count": recorded_anchor_count,
        "followup_anchor_count": followup_anchor_count,
        "full_review_anchor_count": full_review_anchor_count,
        "signoff_anchor_count": signoff_anchor_count,
        "recommendation": "coherence-review",
        "reason": "branch_review_pressure_exceeded",
        "note": (
            f"The branch already carries {commits_since_base} commits since base and {recorded_anchor_count} recorded review anchors "
            f"({followup_anchor_count} follow-up, {full_review_anchor_count} graded full-review, {signoff_anchor_count} signoff). "
            "Stop using narrow interdiff loops on this branch. Run a fresh branch-wide correctness review and consider splitting or checkpointing the reviewed subset before more signoff."
        ),
    }


def best_followup_anchor(*, state: dict[str, Any] | None, review_cwd: Path, head: str) -> dict[str, Any] | None:
    if not state:
        return None
    anchors = [item for item in list(state.get("anchors") or []) if isinstance(item, dict)]
    best_anchor: dict[str, Any] | None = None
    best_distance: int | None = None
    best_index = -1
    for index, raw_anchor in enumerate(anchors):
        reviewed_head = str(raw_anchor.get("reviewed_head") or "").strip()
        if not reviewed_head:
            continue
        try:
            resolved_head = resolve_ref(review_cwd, reviewed_head)
        except ValueError:
            continue
        if not is_ancestor(review_cwd, resolved_head, head):
            continue
        distance = commit_distance(review_cwd, resolved_head, head)
        if best_distance is None or distance < best_distance or (distance == best_distance and index > best_index):
            best_anchor = dict(raw_anchor)
            best_anchor["reviewed_head"] = resolved_head
            best_distance = distance
            best_index = index
    return best_anchor


def inspect_workflow_status(*, state_dir: Path, review_cwd: Path, base: str) -> dict[str, Any]:
    branch = current_branch(review_cwd)
    head = current_head(review_cwd)
    payload: dict[str, Any] = {
        "status": "ok",
        "base": base,
        "branch": branch or "HEAD",
        "head": head,
    }
    state = load_workflow_state(state_dir=state_dir, review_cwd=review_cwd, branch=branch, head=head)
    current_stage_lane = current_stage_full_review_lane(
        state,
        state_dir=state_dir,
        review_cwd=review_cwd,
        branch=branch,
    )
    if current_stage_lane:
        payload["current_stage_lane"] = current_stage_lane
    latest = latest_anchor(state)

    def finalize() -> dict[str, Any]:
        pressure = same_tier_review_pressure(
            state=state,
            state_dir=state_dir,
            review_cwd=review_cwd,
            branch=branch,
        )
        if pressure is not None:
            payload["convergence"] = pressure
        return payload

    def stage_decision(decision: dict[str, Any]) -> dict[str, Any]:
        return add_stage_full_review_lane(
            decision,
            state=state,
            state_dir=state_dir,
            review_cwd=review_cwd,
            branch=branch,
        )

    gate_findings_anchor = latest_unresolved_gate_findings_candidate(
        state_dir=state_dir,
        state=state,
        review_cwd=review_cwd,
        base=base,
        branch=branch,
        head=head,
        current_stage_lane=current_stage_lane,
    )
    if gate_findings_anchor is not None:
        reviewed_head = str(gate_findings_anchor.get("reviewed_head") or "").strip()
        gate_lane = str(gate_findings_anchor.get("lane") or "")
        gate_round_id = str(gate_findings_anchor.get("round_id") or "")
        followup_anchor = gate_findings_anchor.get("followup_anchor") if isinstance(gate_findings_anchor.get("followup_anchor"), dict) else None
        payload["last_reviewed_head"] = reviewed_head
        payload["last_reviewed_lane"] = gate_lane
        payload["last_reviewed_at"] = str(
            gate_findings_anchor.get("decision_recorded_at") or gate_findings_anchor.get("recorded_at") or ""
        )
        payload["last_gate_findings_round_id"] = gate_round_id
        try:
            payload["gate_findings_anchor_not_ancestor"] = not is_ancestor(review_cwd, reviewed_head, head)
        except ValueError:
            payload["gate_findings_anchor_not_ancestor"] = True

        if followup_anchor is not None:
            lane_short = gate_lane.replace("review_", "")
            payload["last_reviewed_head"] = head
            payload["last_reviewed_lane"] = "review-followup"
            payload["last_reviewed_at"] = str(followup_anchor.get("recorded_at") or "")
            payload["source_gate_lane"] = gate_lane
            payload.update(
                {
                    "recommendation": "full-review",
                    "reason": f"{lane_short}_findings_followup_needs_signoff",
                    "recommended_lane": gate_lane,
                    "note": (
                        f"The latest {gate_lane} gate was closed as findings and the current head has a follow-up anchor. "
                        f"Rerun {gate_lane} so all signoff reviewers are effectively green on the current head."
                    ),
                }
            )
            return finalize()

        if reviewed_head == head:
            if has_worktree_changes(review_cwd):
                delta = worktree_diff_stats(review_cwd, head)
                payload.update(delta)
                payload["top_paths"] = [
                    f"{item['path']} (+{item['added']}/-{item['deleted']})"
                    for item in list(delta.get("top_paths") or [])
                ]
                decision = classify_delta_recommendation(delta)
                if str(decision.get("recommendation") or "") == "review-followup":
                    decision = {
                        "recommendation": "review-followup",
                        "reason": "gate_findings_dirty_fix_delta",
                        "note": (
                            "Commit intended follow-up changes or stash unrelated dirty files, then rerun review.py --status. "
                            f"After a clean follow-up, rerun {gate_lane} so both signoff reviewers are green on the current head."
                        ),
                    }
                payload.update(stage_decision(decision))
                return finalize()
            payload.update(
                {
                    "recommendation": "fix-gate-findings",
                    "reason": "gate_findings_current_head",
                    "recommended_lane": "review-followup",
                    "note": (
                        f"The latest {gate_lane} gate was closed as findings on the current head. "
                        "Inspect the stored reviewer output, fix valid findings, then run review-followup on the fix delta before rerunning the gate."
                    ),
                }
            )
            return finalize()

        delta = diff_stats(review_cwd, reviewed_head, "HEAD")
        payload.update(delta)
        payload["top_paths"] = [
            f"{item['path']} (+{item['added']}/-{item['deleted']})"
            for item in list(delta.get("top_paths") or [])
        ]
        decision = classify_delta_recommendation(delta)
        if str(decision.get("recommendation") or "") == "review-followup":
            decision = {
                "recommendation": "review-followup",
                "reason": "gate_findings_fix_delta",
                "note": (
                    f"Use review-followup against the latest {gate_lane} findings head. "
                    f"After a clean follow-up, rerun {gate_lane} so both signoff reviewers are green on the current head."
                ),
            }
        payload.update(stage_decision(decision))
        return finalize()

    if not state or not latest:
        decision = {
            "recommendation": "full-review",
            "reason": "no_review_anchor",
            "note": "No recorded review anchor exists for this branch yet.",
        }
        if current_stage_lane:
            decision["recommended_lane"] = current_stage_lane
            decision["note"] = (
                f"No recorded review anchor exists for this branch yet, but this branch already reached {current_stage_lane}. "
                f"Review-suite lanes are monotonic for a branch; rerun {current_stage_lane} instead of stepping down."
            )
        payload.update(decision)
        return finalize()

    anchor = best_followup_anchor(state=state, review_cwd=review_cwd, head=head)
    if anchor is None:
        latest_reviewed_head = str(latest.get("reviewed_head") or state.get("last_reviewed_head") or "").strip()
        if not latest_reviewed_head:
            payload.update(
                stage_decision(
                    {
                        "recommendation": "full-review",
                        "reason": "invalid_review_anchor",
                        "note": "The latest recorded review anchor does not contain a reviewed head. Run a fresh full review.",
                    }
                )
            )
            return finalize()
        try:
            latest_reviewed_head = resolve_ref(review_cwd, latest_reviewed_head)
        except ValueError:
            payload.update(
                stage_decision(
                    {
                        "recommendation": "full-review",
                        "reason": "review_anchor_unresolvable",
                        "note": "The latest recorded review anchor no longer resolves in this repo. Run a fresh full review.",
                    }
                )
            )
            return finalize()
        if not is_ancestor(review_cwd, latest_reviewed_head, head):
            payload.update(
                stage_decision(
                    {
                        "recommendation": "full-review",
                        "reason": "review_anchor_not_ancestor",
                        "note": "The latest recorded review anchor is no longer an ancestor of HEAD. Run a fresh full review.",
                    }
                )
            )
            return finalize()
        payload.update(
            stage_decision(
                {
                    "recommendation": "full-review",
                    "reason": "invalid_review_anchor",
                    "note": "No valid recorded review anchor remains for this branch state. Run a fresh full review.",
                }
            )
        )
        return finalize()
    reviewed_head = str(anchor.get("reviewed_head") or "").strip()
    payload["last_reviewed_head"] = reviewed_head
    payload["last_reviewed_lane"] = str(anchor.get("lane") or state.get("last_reviewed_lane") or "")
    payload["last_reviewed_at"] = str(anchor.get("recorded_at") or state.get("last_reviewed_at") or "")
    review_scope = dict(anchor.get("review_scope") or {})
    if not reviewed_head:
        payload.update(
            stage_decision(
                {
                    "recommendation": "full-review",
                    "reason": "invalid_review_anchor",
                    "note": "The latest recorded review anchor does not contain a reviewed head. Run a fresh full review.",
                }
            )
        )
        return finalize()
    try:
        reviewed_head = resolve_ref(review_cwd, reviewed_head)
        payload["last_reviewed_head"] = reviewed_head
    except ValueError:
        payload.update(
            stage_decision(
                {
                    "recommendation": "full-review",
                    "reason": "review_anchor_unresolvable",
                    "note": "The latest recorded review anchor no longer resolves in this repo. Run a fresh full review.",
                }
            )
        )
        return finalize()
    base_context_anchor = latest_base_review_context_anchor(state, requested_base=base) or anchor
    base_context_scope = dict(base_context_anchor.get("review_scope") or {})
    recorded_merge_base = str(base_context_scope.get("merge_base") or "").strip()
    stored_anchor_base = str(base_context_scope.get("base") or base_context_anchor.get("base") or state.get("base") or "").strip()
    stored_branch_base = str(base_context_scope.get("branch_base") or "").strip()
    stored_requested_base = str(base_context_scope.get("requested_base") or "").strip()
    requested_status_base = str(base or "").strip()
    if review_scope_matches_requested_base(
        anchor=base_context_anchor,
        review_scope=base_context_scope,
        requested_base=base,
    ):
        if stored_branch_base and requested_status_base in {stored_branch_base, stored_requested_base}:
            anchor_base = stored_branch_base
        else:
            anchor_base = stored_anchor_base or requested_status_base
    else:
        anchor_base = str(base or stored_anchor_base).strip()
    branch_pressure: dict[str, Any] | None = None
    cycle_pressure = followup_cycle_pressure(state=state)
    current_merge_base: str | None = None
    base_drift_patch_equivalent = False
    if recorded_merge_base and anchor_base:
        try:
            current_merge_base = merge_base(review_cwd, anchor_base, "HEAD")
        except ValueError:
            payload.update(
                stage_decision(
                    {
                        "recommendation": "full-review",
                        "reason": "base_merge_base_unresolvable",
                        "note": "The stored review base no longer resolves cleanly in this repo. Run a fresh full review.",
                    }
                )
            )
            return finalize()
        payload["recorded_merge_base"] = recorded_merge_base
        payload["current_merge_base"] = current_merge_base
        if current_merge_base != recorded_merge_base:
            drift_scope = merge_base_drift_scope(
                review_cwd=review_cwd,
                recorded_merge_base=recorded_merge_base,
                current_merge_base=current_merge_base,
                reviewed_head=reviewed_head,
            )
            payload["base_drift_changed_path_count"] = len(drift_scope["base_changed_paths"])
            payload["base_drift_overlap_paths"] = drift_scope["overlapping_paths"][:TOP_PATH_LIMIT]
            payload["base_drift_patch_equivalent"] = bool(drift_scope["patch_equivalent"])
            if drift_scope["overlapping_paths"] or not drift_scope["patch_equivalent"]:
                payload.update(
                    stage_decision(
                        {
                            "recommendation": "full-review",
                            "reason": "base_merge_base_changed",
                            "note": "The branch merge-base against the review base changed and the base drift may affect reviewed branch paths. Run a fresh full review.",
                        }
                    )
                )
                return finalize()
            base_drift_patch_equivalent = True
            reviewed_head = head
            payload["last_reviewed_head"] = head
            payload["base_drift_review_equivalent"] = True
        branch_pressure = branch_review_pressure(
            state=state,
            review_cwd=review_cwd,
            base=anchor_base,
            merge_base_ref=current_merge_base,
        )
    elif anchor_base:
        branch_pressure = branch_review_pressure(
            state=state,
            review_cwd=review_cwd,
            base=anchor_base,
        )
    if not base_drift_patch_equivalent and not is_ancestor(review_cwd, reviewed_head, head):
        payload.update(
            stage_decision(
                {
                    "recommendation": "full-review",
                    "reason": "review_anchor_not_ancestor",
                    "note": "The latest recorded review anchor is no longer an ancestor of HEAD. Run a fresh full review.",
                }
            )
        )
        return finalize()
    if reviewed_head == head:
        if has_worktree_changes(review_cwd):
            if anchor_base:
                dirty_scope = dirty_worktree_scope(
                    review_cwd,
                    anchor_base,
                    merge_base_ref=current_merge_base,
                )
                if bool(dirty_scope.get("all_dirty_paths_outside_branch_diff")):
                    unrelated_paths = list(dirty_scope.get("unrelated_dirty_paths") or [])
                    payload.update(
                        {
                            "worktree_dirty": True,
                            "ignored_dirty_path_count": len(unrelated_paths),
                            "ignored_dirty_paths": unrelated_paths[:TOP_PATH_LIMIT],
                            "recommendation": "none",
                            "reason": "dirty_worktree_outside_branch_diff",
                            "note": "Current HEAD already matches the latest recorded review anchor. Dirty worktree paths are outside the committed branch diff and do not require follow-up.",
                        }
                    )
                    return finalize()
            delta = worktree_diff_stats(review_cwd, head)
            payload.update(delta)
            payload["top_paths"] = [
                f"{item['path']} (+{item['added']}/-{item['deleted']})"
                for item in list(delta.get("top_paths") or [])
            ]
            if cycle_pressure is not None:
                payload.update(stage_decision(cycle_pressure))
                return finalize()
            if branch_pressure is not None:
                payload.update(stage_decision(branch_pressure))
                return finalize()
            decision = classify_delta_recommendation(delta)
            if str(decision.get("recommendation") or "") == "coherence-review":
                payload.update(
                    stage_decision(
                        {
                            "recommendation": "coherence-review",
                            "reason": "dirty_worktree_churn_exceeded",
                            "note": "Current HEAD matches the latest recorded review anchor, but the dirty worktree delta is too large for another narrow follow-up.",
                        }
                    )
                )
                return finalize()
            payload.update(
                {
                    "recommendation": "review-followup",
                    "reason": "dirty_worktree_after_review",
                    "note": "Current HEAD already matches the latest recorded review anchor, but the worktree has uncommitted follow-up changes.",
                }
            )
            return finalize()
        payload.update(
            {
                "recommendation": "none",
                "reason": "current_head_already_reviewed",
                "note": "Current HEAD already matches the latest recorded review anchor.",
            }
        )
        return finalize()
    delta = diff_stats(review_cwd, reviewed_head, "HEAD")
    payload.update(delta)
    payload["top_paths"] = [
        f"{item['path']} (+{item['added']}/-{item['deleted']})"
        for item in list(delta.get("top_paths") or [])
    ]
    if cycle_pressure is not None:
        payload.update(stage_decision(cycle_pressure))
        return finalize()
    if branch_pressure is not None:
        payload.update(stage_decision(branch_pressure))
        return finalize()
    payload.update(stage_decision(classify_delta_recommendation(delta)))
    return finalize()
