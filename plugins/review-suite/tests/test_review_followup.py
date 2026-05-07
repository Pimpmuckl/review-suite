from __future__ import annotations

import sys
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import review_followup


def test_load_followup_note_rejects_conflicting_sources() -> None:
    with pytest.raises(ValueError, match="use either --note or --note-file"):
        review_followup.load_followup_note(note="x", note_file="note.txt")


def test_load_followup_note_requires_non_empty_content(tmp_path: Path) -> None:
    note_path = tmp_path / "note.txt"
    note_path.write_text(" \n\t ", encoding="utf-8")

    with pytest.raises(ValueError, match="follow-up note must not be empty"):
        review_followup.load_followup_note(note=None, note_file=str(note_path))


def test_load_followup_note_resolves_relative_file_against_repo_root(tmp_path: Path) -> None:
    review_root = tmp_path / "repo"
    note_path = review_root / ".review-suite" / "fix-note.md"
    note_path.parent.mkdir(parents=True)
    note_path.write_text("invariant: keep owner state coherent", encoding="utf-8")

    text = review_followup.load_followup_note(
        note=None,
        note_file=".review-suite/fix-note.md",
        review_root=review_root,
    )

    assert text == "invariant: keep owner state coherent"


def test_main_uses_recorded_anchor_and_records_new_followup_anchor(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(review_followup, "resolve_repo_root", lambda cd: tmp_path)
    monkeypatch.setattr(review_followup, "ensure_clean_git_worktree", lambda *args, **kwargs: None)
    monkeypatch.setattr(review_followup, "use_unsafe_windows_wsl_fallback", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        review_followup,
        "inspect_workflow_status",
        lambda **kwargs: {
            "status": "ok",
            "recommendation": "review-followup",
            "last_reviewed_head": "abc123",
        },
    )
    monkeypatch.setattr(review_followup, "current_head", lambda review_root: "def456")
    monkeypatch.setattr(review_followup, "merge_base", lambda review_root, base: "base123")
    monkeypatch.setattr(review_followup, "diff_artifact", lambda review_root, start_ref, end_ref: "diff --git a/x b/x\n")
    def fake_run_codex(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "final_message": "No findings.",
            "session_id": "sess-1",
            "elapsed_seconds": 1.2,
            "timed_out": False,
        }

    monkeypatch.setattr(review_followup, "run_codex", fake_run_codex)
    monkeypatch.setattr(review_followup, "record_review_anchor", lambda **kwargs: captured.setdefault("anchor", kwargs) or {})

    def fake_emit_result(**kwargs):
        captured["result"] = kwargs
        return 0

    monkeypatch.setattr(review_followup, "emit_result", fake_emit_result)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "review_followup.py",
            "--base",
            "main",
            "--note",
            "invariant: branch fix must update the owning state machine",
        ],
    )

    exit_code = review_followup.main()

    assert exit_code == 0
    assert "Fixer root-cause note:" in str(captured["prompt"])
    assert "abc123" in str(captured["prompt"])
    assert "do not stop after the first issue" in str(captured["prompt"])
    assert "unbounded agent-context injection" in str(captured["prompt"])
    assert captured["anchor"]["lane"] == "review-followup"
    assert captured["anchor"]["reviewed_head"] == "def456"
    assert captured["anchor"]["review_scope"]["commit"] == "abc123"
    assert captured["anchor"]["review_scope"]["merge_base"] == "base123"


def test_main_uses_dirty_worktree_followup_when_head_matches_anchor(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(review_followup, "resolve_repo_root", lambda cd: tmp_path)
    monkeypatch.setattr(review_followup, "ensure_clean_git_worktree", lambda *args, **kwargs: None)
    monkeypatch.setattr(review_followup, "use_unsafe_windows_wsl_fallback", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        review_followup,
        "inspect_workflow_status",
        lambda **kwargs: {
            "status": "ok",
            "recommendation": "review-followup",
            "last_reviewed_head": "abc123",
            "worktree_dirty": True,
        },
    )
    monkeypatch.setattr(review_followup, "current_head", lambda review_root: "abc123")
    monkeypatch.setattr(review_followup, "has_worktree_changes", lambda review_root: True)
    monkeypatch.setattr(review_followup, "merge_base", lambda review_root, base: "base123")
    monkeypatch.setattr(
        review_followup,
        "worktree_diff_artifact",
        lambda review_root, anchor_ref="HEAD": "diff --git a/x b/x\n",
    )

    def fake_run_codex(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "final_message": "No findings.",
            "session_id": "sess-1",
            "elapsed_seconds": 1.2,
            "timed_out": False,
        }

    monkeypatch.setattr(review_followup, "run_codex", fake_run_codex)
    monkeypatch.setattr(review_followup, "record_review_anchor", lambda **kwargs: captured.setdefault("anchor", kwargs) or {})

    def fake_emit_result(**kwargs):
        captured["result"] = kwargs
        return 0

    monkeypatch.setattr(review_followup, "emit_result", fake_emit_result)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "review_followup.py",
            "--base",
            "main",
            "--allow-dirty",
            "--note",
            "invariant: keep the dirty follow-up routed through the same reviewed head",
        ],
    )

    exit_code = review_followup.main()

    assert exit_code == 0
    assert "dirty follow-up diff against HEAD `abc123`" in str(captured["prompt"])
    assert captured["anchor"]["review_scope"]["dirty_worktree"] is True
    assert captured["anchor"]["review_scope"]["commit"] == "abc123"
    assert "commit_end" not in captured["anchor"]["review_scope"]


def test_resolve_since_head_requires_anchor_when_not_explicit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(review_followup, "inspect_workflow_status", lambda **kwargs: {"status": "ok"})
    with pytest.raises(ValueError, match="requires --since or an existing recorded review anchor"):
        review_followup.resolve_since_head(
            explicit_since=None,
            state_dir=tmp_path / "state",
            review_cwd=tmp_path,
            base="main",
            force=False,
        )


def test_resolve_since_head_rejects_large_recorded_delta_without_force(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        review_followup,
        "inspect_workflow_status",
        lambda **kwargs: {
            "status": "ok",
            "recommendation": "coherence-review",
            "last_reviewed_head": "abc123",
            "note": "The post-review delta is large enough that coherence/reset review is safer than another narrow follow-up.",
        },
    )

    with pytest.raises(ValueError, match="coherence/reset"):
        review_followup.resolve_since_head(
            explicit_since=None,
            state_dir=tmp_path / "state",
            review_cwd=tmp_path,
            base="main",
            force=False,
        )


def test_resolve_since_head_allows_large_explicit_delta_with_force(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        review_followup,
        "diff_stats",
        lambda review_cwd, start_ref, end_ref: {"commits_since_anchor": 8, "files_changed": 9, "lines_changed": 900},
    )

    resolved = review_followup.resolve_since_head(
        explicit_since="abc123",
        state_dir=tmp_path / "state",
        review_cwd=tmp_path,
        base="main",
        force=True,
    )

    assert resolved == "abc123"


def test_resolve_since_head_allows_valid_explicit_anchor_even_when_stored_state_is_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        review_followup,
        "inspect_workflow_status",
        lambda **kwargs: {
            "status": "ok",
            "recommendation": "full-review",
            "reason": "review_anchor_not_ancestor",
            "note": "The latest recorded review anchor is stale.",
        },
    )
    monkeypatch.setattr(
        review_followup,
        "resolve_ref",
        lambda review_cwd, ref: "abc123-resolved",
    )
    monkeypatch.setattr(review_followup, "is_ancestor", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        review_followup,
        "diff_stats",
        lambda review_cwd, start_ref, end_ref: {"commits_since_anchor": 1, "files_changed": 1, "lines_changed": 20},
    )

    resolved = review_followup.resolve_since_head(
        explicit_since="abc123",
        state_dir=tmp_path / "state",
        review_cwd=tmp_path,
        base="main",
        force=False,
    )

    assert resolved == "abc123-resolved"


def test_resolve_since_head_rejects_explicit_anchor_when_branch_review_pressure_is_high(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        review_followup,
        "inspect_workflow_status",
        lambda **kwargs: {
            "status": "ok",
            "recommendation": "coherence-review",
            "reason": "branch_review_pressure_exceeded",
            "note": "The branch already carries too much review churn for another narrow follow-up.",
        },
    )

    with pytest.raises(ValueError, match="too much review churn"):
        review_followup.resolve_since_head(
            explicit_since="abc123",
            state_dir=tmp_path / "state",
            review_cwd=tmp_path,
            base="main",
            force=False,
        )


def test_resolve_since_head_rejects_explicit_anchor_when_followup_cycle_limit_is_exceeded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        review_followup,
        "inspect_workflow_status",
        lambda **kwargs: {
            "status": "ok",
            "recommendation": "coherence-review",
            "reason": "followup_cycle_limit_exceeded",
            "note": "The branch already used too many follow-up rounds since the last full checkpoint.",
        },
    )

    with pytest.raises(ValueError, match="too many follow-up rounds"):
        review_followup.resolve_since_head(
            explicit_since="abc123",
            state_dir=tmp_path / "state",
            review_cwd=tmp_path,
            base="main",
            force=False,
        )


def test_resolve_since_head_rejects_explicit_anchor_that_is_not_ancestor_without_force(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(review_followup, "inspect_workflow_status", lambda **kwargs: {"status": "ok"})
    monkeypatch.setattr(review_followup, "resolve_ref", lambda review_cwd, ref: "abc123-resolved")
    monkeypatch.setattr(review_followup, "is_ancestor", lambda *args, **kwargs: False)

    with pytest.raises(ValueError, match="ancestor of HEAD"):
        review_followup.resolve_since_head(
            explicit_since="abc123",
            state_dir=tmp_path / "state",
            review_cwd=tmp_path,
            base="main",
            force=False,
        )


def test_resolve_since_head_allows_non_ancestor_gate_findings_anchor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        review_followup,
        "inspect_workflow_status",
        lambda **kwargs: {
            "status": "ok",
            "recommendation": "review-followup",
            "reason": "gate_findings_fix_delta",
            "last_reviewed_head": "abc123-resolved",
        },
    )
    monkeypatch.setattr(review_followup, "resolve_ref", lambda review_cwd, ref: "abc123-resolved")
    monkeypatch.setattr(review_followup, "is_ancestor", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        review_followup,
        "diff_stats",
        lambda review_cwd, start_ref, end_ref: {"commits_since_anchor": 1, "files_changed": 1, "lines_changed": 20},
    )

    resolved = review_followup.resolve_since_head(
        explicit_since="abc123",
        state_dir=tmp_path / "state",
        review_cwd=tmp_path,
        base="main",
        force=False,
    )

    assert resolved == "abc123-resolved"


def test_gate_findings_source_context_returns_link_metadata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        review_followup,
        "inspect_workflow_status",
        lambda **kwargs: {
            "reason": "gate_findings_fix_delta",
            "last_reviewed_head": "old-head",
            "last_reviewed_lane": "review_t4",
            "last_gate_findings_round_id": "gate-round-1",
        },
    )
    monkeypatch.setattr(
        review_followup,
        "resolve_ref",
        lambda review_cwd, ref: {"old-head": "old-sha", "since-ref": "old-sha"}[ref],
    )

    context = review_followup.gate_findings_source_context(
        state_dir=tmp_path / "state",
        review_cwd=tmp_path,
        base="main",
        since_head="since-ref",
    )

    assert context == {
        "source_gate_round_id": "gate-round-1",
        "source_gate_lane": "review_t4",
        "source_gate_reviewed_head": "old-sha",
    }


def test_resolve_since_head_rejects_large_explicit_delta_without_force(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(review_followup, "inspect_workflow_status", lambda **kwargs: {"status": "ok"})
    monkeypatch.setattr(review_followup, "resolve_ref", lambda review_cwd, ref: "abc123-resolved")
    monkeypatch.setattr(review_followup, "is_ancestor", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        review_followup,
        "diff_stats",
        lambda review_cwd, start_ref, end_ref: {"commits_since_anchor": 8, "files_changed": 9, "lines_changed": 900},
    )

    with pytest.raises(ValueError, match="coherence/reset"):
        review_followup.resolve_since_head(
            explicit_since="abc123",
            state_dir=tmp_path / "state",
            review_cwd=tmp_path,
            base="main",
            force=False,
        )
