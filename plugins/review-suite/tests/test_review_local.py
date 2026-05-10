from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import review_deslop
import review_followup
import review_github
import review_plan
import review_suite_arena
import review_state
import review_t1
import review_t2
import review_t3
import review_t4
import review_suite_local

from review_suite_local import build_local_review_request, build_manual_review_prompt, build_phase_instructions, build_pr_instructions, load_custom_instructions, uses_native_base_review


def _subparser_help(parser: argparse.ArgumentParser, name: str) -> str:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices[name].format_help()
    raise AssertionError(f"subparser not found: {name}")


def test_load_custom_instructions_rejects_conflicting_sources() -> None:
    with pytest.raises(ValueError, match="use either --instructions or --instructions-file"):
        load_custom_instructions(instructions="focus on correctness", instructions_file="instructions.txt")


@pytest.mark.parametrize(
    ("instructions", "file_body"),
    [
        ("   ", None),
        (None, " \n\t "),
    ],
)
def test_load_custom_instructions_rejects_empty_content(
    tmp_path: Path,
    instructions: str | None,
    file_body: str | None,
) -> None:
    instructions_file = None
    if file_body is not None:
        path = tmp_path / "instructions.txt"
        path.write_text(file_body, encoding="utf-8")
        instructions_file = str(path)

    with pytest.raises(ValueError, match="custom instructions must not be empty"):
        load_custom_instructions(instructions=instructions, instructions_file=instructions_file)


def test_standard_review_contract_includes_current_codex_review_dimensions() -> None:
    prompt = build_phase_instructions("commit `abc123`")

    assert "Reviewer output is advisory risk input, not authoritative product direction" in prompt
    assert "do not stop after the first issue" in prompt
    assert "line number when available" in prompt
    assert "A finding is only valid" in prompt
    assert "UX preference" in prompt
    assert "backwards-compat speculation" in prompt
    assert "oversized or hard-to-stage diffs" in prompt
    assert "external integration surface breaks" in prompt
    assert "missing regression or integration coverage" in prompt
    assert "unbounded agent-context injection" in prompt
    assert "Do not assume backwards compatibility" in prompt
    assert "Scope questions / suggestions (non-findings)" in prompt
    assert "Do not recommend code changes that reverse explicit product intent" in prompt
    assert "No findings." in prompt


def test_manual_review_prompt_treats_reviewer_output_as_advisory() -> None:
    prompt = build_manual_review_prompt(
        instructions="Review the supplied diff.",
        diff_text="diff --git a/app.py b/app.py\n",
    )

    assert "Reviewer output is advisory risk input, not authoritative product direction" in prompt
    assert "concrete correctness, regression, integration, security, accessibility, or maintainability risk" in prompt
    assert "UX preferences, product-scope speculation, backwards-compat speculation, and alternative product direction are non-findings" in prompt
    assert "Scope questions / suggestions (non-findings)" in prompt
    assert "Do not recommend code changes that reverse explicit product intent" in prompt


def test_build_local_review_request_appends_custom_block_after_standard_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        assert args[:2] == ["git", "show"]
        return subprocess.CompletedProcess(args, 0, stdout="diff --git a/x b/x\n", stderr="")

    monkeypatch.setattr(review_suite_local.subprocess, "run", fake_run)

    request = build_local_review_request(
        review_cwd=tmp_path,
        base="main",
        commit_values=["abc123"],
        instruction_builder=build_phase_instructions,
        custom_instructions="Do not spend time on backwards compatibility concerns.",
    )

    standard = build_phase_instructions("commit `abc123`")
    assert calls == [["git", "show", "--stat", "--patch", "--find-renames", "abc123"]]
    assert request.review_scope["commit"] == "abc123"
    assert not uses_native_base_review(request.review_scope)
    assert request.prompt.index(standard) < request.prompt.index("Additional review instructions:")
    assert "Do not spend time on backwards compatibility concerns." in request.prompt
    assert "=== BEGIN DIFF ===" in request.prompt


def test_build_local_review_request_pr_base_without_custom_instructions_stays_native(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        if args[:2] == ["git", "merge-base"]:
            return subprocess.CompletedProcess(args, 0, stdout="merge-base-sha\n", stderr="")
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, stdout="head-sha\n", stderr="")
        raise AssertionError(args)

    monkeypatch.setattr(review_suite_local.subprocess, "run", fake_run)
    monkeypatch.setattr(
        review_suite_local,
        "dirty_worktree_scope",
        lambda review_cwd, base: {"all_dirty_paths_outside_branch_diff": False},
    )

    request = build_local_review_request(
        review_cwd=tmp_path,
        base="main",
        commit_values=None,
        instruction_builder=build_pr_instructions,
        custom_instructions=None,
    )

    assert calls == [["git", "merge-base", "main", "HEAD"], ["git", "rev-parse", "HEAD"]]
    assert uses_native_base_review(request.review_scope)
    assert request.review_scope["base"] == "main"
    assert request.review_scope["merge_base"] == "merge-base-sha"
    assert request.review_scope["reviewed_head"] == "head-sha"
    assert request.prompt == ""


def test_build_local_review_request_pr_base_with_custom_instructions_uses_merge_base_diff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        if args[:2] == ["git", "merge-base"]:
            return subprocess.CompletedProcess(args, 0, stdout="merge-base-sha\n", stderr="")
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, stdout="head-sha\n", stderr="")
        if args[:2] == ["git", "diff"]:
            return subprocess.CompletedProcess(args, 0, stdout="diff --git a/x b/x\n", stderr="")
        raise AssertionError(args)

    monkeypatch.setattr(review_suite_local.subprocess, "run", fake_run)

    request = build_local_review_request(
        review_cwd=tmp_path,
        base="main",
        commit_values=None,
        instruction_builder=build_pr_instructions,
        custom_instructions="Stay focused on correctness only.",
    )

    assert calls[0] == ["git", "merge-base", "main", "HEAD"]
    assert calls[1] == ["git", "rev-parse", "HEAD"]
    assert calls[2] == ["git", "diff", "--find-renames", "--stat", "--patch", "merge-base-sha..HEAD"]
    assert not any("main..HEAD" in " ".join(call) for call in calls)
    assert request.review_scope["base"] == "main"
    assert request.review_scope["manual_prompt_mode"] is True
    assert request.review_scope["merge_base"] == "merge-base-sha"
    assert request.review_scope["reviewed_head"] == "head-sha"
    assert not uses_native_base_review(request.review_scope)
    assert "Review this PR-ready branch diff for correctness and regression risk." in request.prompt
    assert "Additional review instructions:" in request.prompt
    assert "branch diff against base `main`" in request.prompt


def test_build_local_review_request_pr_base_without_custom_instructions_stays_native_for_unrelated_dirty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        if args[:2] == ["git", "merge-base"]:
            return subprocess.CompletedProcess(args, 0, stdout="merge-base-sha\n", stderr="")
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, stdout="head-sha\n", stderr="")
        raise AssertionError(args)

    monkeypatch.setattr(review_suite_local.subprocess, "run", fake_run)
    monkeypatch.setattr(
        review_suite_local,
        "dirty_worktree_scope",
        lambda review_cwd, base: {
            "all_dirty_paths_outside_branch_diff": True,
            "unrelated_dirty_paths": ["docs/notes.md"],
        },
    )

    request = build_local_review_request(
        review_cwd=tmp_path,
        base="main",
        commit_values=None,
        instruction_builder=build_pr_instructions,
        custom_instructions=None,
    )

    assert uses_native_base_review(request.review_scope) is True
    assert request.review_scope["base"] == "main"
    assert request.review_scope["ignored_dirty_path_count"] == 1
    assert request.review_scope["ignored_dirty_paths"] == ["docs/notes.md"]
    assert request.prompt == ""


def test_build_local_review_request_rejects_oversized_manual_diff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_run(args, **kwargs):
        if args[:2] == ["git", "merge-base"]:
            return subprocess.CompletedProcess(args, 0, stdout="merge-base-sha\n", stderr="")
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, stdout="head-sha\n", stderr="")
        if args[:2] == ["git", "diff"]:
            return subprocess.CompletedProcess(args, 0, stdout=f"diff --git a/x b/x\n+{'x' * 200}\n", stderr="")
        raise AssertionError(args)

    monkeypatch.setattr(review_suite_local.subprocess, "run", fake_run)
    monkeypatch.setattr(review_suite_local, "MANUAL_REVIEW_PROMPT_MAX_CHARS", 100)
    monkeypatch.setattr(
        review_suite_local,
        "dirty_worktree_scope",
        lambda review_cwd, base: {
            "all_dirty_paths_outside_branch_diff": True,
            "unrelated_dirty_paths": ["docs/notes.md"],
        },
    )

    with pytest.raises(ValueError, match="Split the change into a smaller review slice"):
        build_local_review_request(
            review_cwd=tmp_path,
            base="main",
            commit_values=None,
            instruction_builder=build_pr_instructions,
            custom_instructions="Stay focused on correctness.",
        )


@pytest.mark.parametrize(
    ("module", "runner_attr"),
    [
        (review_t1, "run_benchmarked_round"),
        (review_t2, "run_gate_round"),
    ],
)
def test_t1_t2_commit_review_with_custom_instructions_stays_manual(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module,
    runner_attr: str,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(args, **kwargs):
        assert args[:2] == ["git", "show"]
        return subprocess.CompletedProcess(args, 0, stdout="diff --git a/x b/x\n", stderr="")

    monkeypatch.setattr(review_suite_local.subprocess, "run", fake_run)
    monkeypatch.setattr(
        review_suite_local,
        "dirty_worktree_scope",
        lambda review_cwd, base: {"all_dirty_paths_outside_branch_diff": False},
    )
    monkeypatch.setattr(module, "resolve_repo_root", lambda cd: tmp_path)
    if module is review_t1:
        monkeypatch.setattr(module, "resolve_caller_id", lambda caller_id: ("caller-1", "explicit"))
        monkeypatch.setattr(module, runner_attr, lambda **kwargs: captured.update(kwargs) or 0)
    else:
        monkeypatch.setattr(module, "emit_toon", lambda payload: None)
        monkeypatch.setattr(module, runner_attr, lambda **kwargs: (captured.update(kwargs) or {"status": "ok"}, 0))
    monkeypatch.setattr(
        sys,
        "argv",
        [f"{module.__name__}.py", "--commit", "abc123", "--instructions", "Do not talk about backwards compatibility."],
    )

    exit_code = module.main()

    assert exit_code == 0
    assert captured["review_scope"]["commit"] == "abc123"
    assert "Additional review instructions:" in str(captured["prompt"])
    assert "Do not talk about backwards compatibility." in str(captured["prompt"])


@pytest.mark.parametrize(
    ("module", "runner_attr"),
    [
        (review_t3, "run_benchmarked_round"),
        (review_t4, "run_gate_round"),
    ],
)
def test_t3_t4_without_custom_instructions_keep_native_base_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module,
    runner_attr: str,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(args, **kwargs):
        if args[:2] == ["git", "merge-base"]:
            return subprocess.CompletedProcess(args, 0, stdout="merge-base-sha\n", stderr="")
        if args[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return subprocess.CompletedProcess(args, 0, stdout="feature/test\n", stderr="")
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, stdout="head-sha\n", stderr="")
        raise AssertionError(args)

    monkeypatch.setattr(review_suite_local.subprocess, "run", fake_run)
    monkeypatch.setattr(
        review_suite_local,
        "dirty_worktree_scope",
        lambda review_cwd, base: {"all_dirty_paths_outside_branch_diff": False},
    )
    monkeypatch.setattr(module, "resolve_repo_root", lambda cd: tmp_path)
    if module is review_t3:
        monkeypatch.setattr(module, "resolve_caller_id", lambda caller_id: ("caller-1", "explicit"))
        monkeypatch.setattr(module, runner_attr, lambda **kwargs: captured.update(kwargs) or 0)
    else:
        monkeypatch.setattr(module, "guard_branch_signoff_lane", lambda **kwargs: None)
        monkeypatch.setattr(module, "emit_toon", lambda payload: None)
        monkeypatch.setattr(module, runner_attr, lambda **kwargs: (captured.update(kwargs) or {"status": "ok"}, 0))
    monkeypatch.setattr(sys, "argv", [f"{module.__name__}.py", "--base", "main"])

    exit_code = module.main()

    assert exit_code == 0
    assert uses_native_base_review(captured["review_scope"]) is True
    assert captured["review_scope"]["base"] == "main"
    assert captured["review_scope"]["merge_base"] == "merge-base-sha"
    assert captured["review_scope"]["reviewed_head"] == "head-sha"
    assert captured["prompt"] == ""


@pytest.mark.parametrize(
    ("module", "argv"),
    [
        (review_t2, ["review_t2.py", "--commit", "abc123", "--champion-override", "gpt-5.5-medium"]),
        (review_t4, ["review_t4.py", "--base", "main", "--champion-override", "gpt-5.5-xhigh"]),
    ],
)
def test_t2_t4_pass_champion_override_to_gate_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module,
    argv: list[str],
) -> None:
    captured: dict[str, object] = {}

    def fake_run(args, **kwargs):
        if args[:2] == ["git", "show"]:
            return subprocess.CompletedProcess(args, 0, stdout="diff --git a/x b/x\n", stderr="")
        if args[:2] == ["git", "merge-base"]:
            return subprocess.CompletedProcess(args, 0, stdout="merge-base-sha\n", stderr="")
        if args[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return subprocess.CompletedProcess(args, 0, stdout="feature/test\n", stderr="")
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, stdout="head-sha\n", stderr="")
        raise AssertionError(args)

    monkeypatch.setattr(review_suite_local.subprocess, "run", fake_run)
    monkeypatch.setattr(
        review_suite_local,
        "dirty_worktree_scope",
        lambda review_cwd, base: {"all_dirty_paths_outside_branch_diff": False},
    )
    monkeypatch.setattr(module, "resolve_repo_root", lambda cd: tmp_path)
    if module is review_t4:
        monkeypatch.setattr(module, "guard_branch_signoff_lane", lambda **kwargs: None)
    monkeypatch.setattr(module, "emit_toon", lambda payload: None)
    monkeypatch.setattr(module, "run_gate_round", lambda **kwargs: (captured.update(kwargs) or {"status": "ok"}, 0))
    monkeypatch.setattr(sys, "argv", argv)

    exit_code = module.main()

    assert exit_code == 0
    assert captured["champion_override"] == argv[-1]


@pytest.mark.parametrize(
    ("module", "runner_attr"),
    [
        (review_t3, "run_benchmarked_round"),
        (review_t4, "run_gate_round"),
    ],
)
def test_t3_t4_with_custom_instructions_switch_to_manual_merge_base_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module,
    runner_attr: str,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(args, **kwargs):
        if args[:2] == ["git", "merge-base"]:
            return subprocess.CompletedProcess(args, 0, stdout="merge-base-sha\n", stderr="")
        if args[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return subprocess.CompletedProcess(args, 0, stdout="feature/test\n", stderr="")
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, stdout="head-sha\n", stderr="")
        if args[:2] == ["git", "diff"]:
            return subprocess.CompletedProcess(args, 0, stdout="diff --git a/x b/x\n", stderr="")
        raise AssertionError(args)

    monkeypatch.setattr(review_suite_local.subprocess, "run", fake_run)
    monkeypatch.setattr(module, "resolve_repo_root", lambda cd: tmp_path)
    if module is review_t3:
        monkeypatch.setattr(module, "resolve_caller_id", lambda caller_id: ("caller-1", "explicit"))
        monkeypatch.setattr(module, runner_attr, lambda **kwargs: captured.update(kwargs) or 0)
    else:
        monkeypatch.setattr(module, "guard_branch_signoff_lane", lambda **kwargs: None)
        monkeypatch.setattr(module, "emit_toon", lambda payload: None)
        monkeypatch.setattr(module, runner_attr, lambda **kwargs: (captured.update(kwargs) or {"status": "ok"}, 0))
    monkeypatch.setattr(
        sys,
        "argv",
        [f"{module.__name__}.py", "--base", "main", "--instructions", "Ignore backwards compatibility."]
    )

    exit_code = module.main()

    assert exit_code == 0
    assert uses_native_base_review(captured["review_scope"]) is False
    assert captured["review_scope"]["base"] == "main"
    assert captured["review_scope"]["manual_prompt_mode"] is True
    assert captured["review_scope"]["reviewed_head"] == "head-sha"
    assert "Additional review instructions:" in str(captured["prompt"])
    assert "Ignore backwards compatibility." in str(captured["prompt"])


def test_review_plan_deslop_and_github_do_not_expose_custom_instruction_flags() -> None:
    assert "--instructions" not in review_plan.build_parser().format_help()
    assert "--instructions-file" not in review_plan.build_parser().format_help()
    assert "--instructions" not in review_deslop.build_parser().format_help()
    assert "--instructions-file" not in review_deslop.build_parser().format_help()
    assert "--instructions" not in review_github.build_parser().format_help()
    assert "--instructions-file" not in review_github.build_parser().format_help()


def test_agent_wrapper_help_omits_low_roi_runtime_knobs() -> None:
    removed_flags = (
        "--model",
        "--reasoning-effort",
        "--progress-interval-seconds",
        "--timeout-seconds",
        "--poll-seconds",
        "--timeout-minutes",
        "--status-interval-seconds",
        "--re-request-after-seconds",
        "--max-request-attempts",
        "--settle-seconds",
    )
    help_texts = [
        review_plan.build_parser().format_help(),
        review_deslop.build_parser().format_help(),
        review_followup.build_parser().format_help(),
        review_t1.build_parser().format_help(),
        review_t2.build_parser().format_help(),
        review_t3.build_parser().format_help(),
        review_t4.build_parser().format_help(),
        _subparser_help(review_github.build_parser(), "run"),
        _subparser_help(review_suite_arena.build_parser(), "run"),
        _subparser_help(review_suite_arena.build_parser(), "run-round"),
        _subparser_help(review_suite_arena.build_parser(), "resume-round"),
        _subparser_help(review_suite_arena.build_parser(), "run-manual-round"),
        _subparser_help(review_suite_arena.build_parser(), "reroll-slot"),
    ]

    for help_text in help_texts:
        for flag in removed_flags:
            assert flag not in help_text


def test_primary_wrappers_hide_operator_state_knobs_from_help() -> None:
    hidden_flags = (
        "--task-id",
        "--seed",
        "--roster",
        "--rubric",
        "--state-dir",
        "--sqlite-path",
        "--caller-id",
        "--ignore-pending-grades",
        "--winner",
        "--basis",
        "--alpha-note",
        "--bravo-note",
        "--bot-login",
    )
    help_texts = [
        review_followup.build_parser().format_help(),
        review_t1.build_parser().format_help(),
        review_t2.build_parser().format_help(),
        review_t3.build_parser().format_help(),
        review_t4.build_parser().format_help(),
        _subparser_help(review_github.build_parser(), "run"),
        _subparser_help(review_state.build_parser(), "status"),
    ]

    for help_text in help_texts:
        for flag in hidden_flags:
            assert flag not in help_text


def test_agent_wrapper_help_keeps_useful_targeting_controls_visible() -> None:
    assert "--input-file" in review_plan.build_parser().format_help()
    assert "--focus" in review_deslop.build_parser().format_help()
    assert "--note-file" in review_followup.build_parser().format_help()
    assert "--commit" in review_t1.build_parser().format_help()
    assert "--commit" in review_t2.build_parser().format_help()
    assert "--instructions-file" in review_t3.build_parser().format_help()
    assert "--instructions-file" in review_t4.build_parser().format_help()
    assert "--pr-number" in _subparser_help(review_github.build_parser(), "run")
    assert "--base" in _subparser_help(review_state.build_parser(), "status")


def test_arena_wrapper_help_mentions_blocking_round_recovery() -> None:
    for help_text in (
        review_t1.build_parser().format_help(),
        review_t3.build_parser().format_help(),
    ):
        assert "blocks this wrapper" in help_text
        assert "round id" in help_text
        assert "grade_command" in help_text
        assert "dismiss_command" in help_text
        assert "review_state.py status" in help_text


def test_local_review_wrappers_expose_short_wsl_flag() -> None:
    for help_text in (
        review_t1.build_parser().format_help(),
        review_t2.build_parser().format_help(),
        review_t3.build_parser().format_help(),
        review_t4.build_parser().format_help(),
        review_followup.build_parser().format_help(),
        review_plan.build_parser().format_help(),
        review_deslop.build_parser().format_help(),
    ):
        assert "--wsl" in help_text
        assert "--allow-unsafe-windows-codex-wsl-fallback" not in help_text


def test_guard_branch_signoff_lane_rejects_followup_drift_for_current_branch_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        review_suite_local,
        "inspect_workflow_status",
        lambda **kwargs: {
            "recommendation": "review-followup",
            "note": "Use the interdiff follow-up lane against the last reviewed head.",
        },
    )

    with pytest.raises(ValueError, match="requires review-followup"):
        review_suite_local.guard_branch_signoff_lane(
            lane="review_t2",
            review_cwd=tmp_path,
            base="main",
            state_dir=tmp_path / "state",
            review_scope={"base": "main"},
        )

    with pytest.raises(ValueError, match="requires review-followup"):
        review_suite_local.guard_branch_signoff_lane(
            lane="review_t4",
            review_cwd=tmp_path,
            base="main",
            state_dir=tmp_path / "state",
            review_scope={"base": "main"},
        )


def test_guard_branch_signoff_lane_allows_stage_reset_lane(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        review_suite_local,
        "inspect_workflow_status",
        lambda **kwargs: {
            "recommendation": "coherence-review",
            "recommended_lane": "review_t4",
            "note": "Run the full-diff gate lane for this stage.",
        },
    )

    review_suite_local.guard_branch_signoff_lane(
        lane="review_t4",
        review_cwd=tmp_path,
        base="main",
        state_dir=tmp_path / "state",
        review_scope={"base": "main"},
    )


def test_guard_branch_signoff_lane_skips_commit_scope(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        review_suite_local,
        "inspect_workflow_status",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("commit scope should not inspect branch workflow state")),
    )

    review_suite_local.guard_branch_signoff_lane(
        lane="review_t2",
        review_cwd=tmp_path,
        base="main",
        state_dir=tmp_path / "state",
        review_scope={"commit": "abc123"},
    )


@pytest.mark.parametrize("module", [review_t2, review_t4])
def test_gate_wrappers_fail_fast_when_branch_signoff_guard_blocks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module,
) -> None:
    captured_errors: list[str] = []

    monkeypatch.setattr(module, "resolve_repo_root", lambda cd: tmp_path)
    monkeypatch.setattr(
        module,
        "build_local_review_request",
        lambda **kwargs: SimpleNamespace(review_scope={"base": "main"}, prompt=""),
    )
    monkeypatch.setattr(
        module,
        "guard_branch_signoff_lane",
        lambda **kwargs: (_ for _ in ()).throw(ValueError("guard blocked this signoff")),
    )
    monkeypatch.setattr(
        module,
        "run_gate_round",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("guarded signoff must not launch gate reviewers")),
    )
    monkeypatch.setattr(module, "emit_error", lambda message, **kwargs: captured_errors.append(message) or 2)
    monkeypatch.setattr(sys, "argv", [f"{module.__name__}.py", "--base", "main"])

    exit_code = module.main()

    assert exit_code == 2
    assert captured_errors == ["guard blocked this signoff"]


@pytest.mark.parametrize("module", [review_t2, review_t4])
def test_gate_wrappers_fail_fast_when_stage_step_down_guard_blocks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module,
) -> None:
    captured_errors: list[str] = []

    monkeypatch.setattr(module, "resolve_repo_root", lambda cd: tmp_path)
    monkeypatch.setattr(
        module,
        "build_local_review_request",
        lambda **kwargs: SimpleNamespace(review_scope={"base": "main"}, prompt=""),
    )
    monkeypatch.setattr(
        module,
        "guard_no_stage_step_down",
        lambda **kwargs: (_ for _ in ()).throw(ValueError("stage step-down blocked")),
    )
    monkeypatch.setattr(
        module,
        "guard_branch_signoff_lane",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("stage guard should run first")),
    )
    monkeypatch.setattr(
        module,
        "run_gate_round",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("guarded signoff must not launch gate reviewers")),
    )
    monkeypatch.setattr(module, "emit_error", lambda message, **kwargs: captured_errors.append(message) or 2)
    monkeypatch.setattr(sys, "argv", [f"{module.__name__}.py", "--base", "main"])

    exit_code = module.main()

    assert exit_code == 2
    assert captured_errors == ["stage step-down blocked"]


@pytest.mark.parametrize("module", [review_t2, review_t4])
def test_gate_wrappers_stage_step_down_escape_hatch_bypasses_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module,
) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(module, "resolve_repo_root", lambda cd: tmp_path)
    monkeypatch.setattr(
        module,
        "build_local_review_request",
        lambda **kwargs: SimpleNamespace(review_scope={"base": "main"}, prompt=""),
    )
    monkeypatch.setattr(
        module,
        "guard_no_stage_step_down",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("escape hatch should bypass stage guard")),
    )
    monkeypatch.setattr(module, "guard_branch_signoff_lane", lambda **kwargs: None)
    monkeypatch.setattr(module, "run_gate_round", lambda **kwargs: (captured.update(kwargs) or {"status": "completed"}, 0))
    monkeypatch.setattr(module, "output_isatty", lambda: True)
    monkeypatch.setattr(sys, "argv", [f"{module.__name__}.py", "--base", "main", "--allow-stage-step-down"])

    exit_code = module.main()

    assert exit_code == 0
    assert captured["review_scope"]["base"] == "main"


@pytest.mark.parametrize("module", [review_t2, review_t4])
def test_gate_wrappers_skip_final_toon_in_live_tty_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, module) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(module, "resolve_repo_root", lambda cd: tmp_path)
    monkeypatch.setattr(
        module,
        "build_local_review_request",
        lambda **kwargs: SimpleNamespace(review_scope={"base": "main"}, prompt=""),
    )
    monkeypatch.setattr(
        module,
        "run_gate_round",
        lambda **kwargs: (captured.update(kwargs) or {"status": "completed"}, 0),
    )
    monkeypatch.setattr(module, "guard_branch_signoff_lane", lambda **kwargs: None)
    monkeypatch.setattr(module, "output_isatty", lambda: True)
    monkeypatch.setattr(
        module,
        "emit_toon",
        lambda payload: (_ for _ in ()).throw(AssertionError("live gate wrapper should not emit final TOON")),
    )
    monkeypatch.setattr(sys, "argv", [f"{module.__name__}.py", "--base", "main"])

    exit_code = module.main()

    assert exit_code == 0
    assert captured["review_scope"]["base"] == "main"


@pytest.mark.parametrize("module", [review_t2, review_t4])
def test_gate_wrappers_emit_signoff_pending_toon_even_in_live_tty_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module,
) -> None:
    emitted: list[dict[str, object]] = []

    monkeypatch.setattr(module, "resolve_repo_root", lambda cd: tmp_path)
    monkeypatch.setattr(
        module,
        "build_local_review_request",
        lambda **kwargs: SimpleNamespace(review_scope={"base": "main"}, prompt=""),
    )
    monkeypatch.setattr(
        module,
        "run_gate_round",
        lambda **kwargs: (
            {"status": "signoff_pending", "blocked": False, "action": {"lane": "gate-signoff"}},
            0,
        ),
    )
    monkeypatch.setattr(module, "guard_branch_signoff_lane", lambda **kwargs: None)
    monkeypatch.setattr(module, "output_isatty", lambda: True)
    monkeypatch.setattr(module, "emit_toon", lambda payload: emitted.append(payload))
    monkeypatch.setattr(sys, "argv", [f"{module.__name__}.py", "--base", "main"])

    exit_code = module.main()

    assert exit_code == 0
    assert emitted == [{"status": "signoff_pending", "blocked": False, "action": {"lane": "gate-signoff"}}]


@pytest.mark.parametrize("module", [review_t2, review_t4])
def test_gate_wrappers_skip_final_toon_for_completed_noninteractive_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module,
) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(module, "resolve_repo_root", lambda cd: tmp_path)
    monkeypatch.setattr(
        module,
        "build_local_review_request",
        lambda **kwargs: SimpleNamespace(review_scope={"base": "main"}, prompt=""),
    )
    monkeypatch.setattr(
        module,
        "run_gate_round",
        lambda **kwargs: (captured.update(kwargs) or {"status": "completed", "blocked": False}, 0),
    )
    monkeypatch.setattr(module, "guard_branch_signoff_lane", lambda **kwargs: None)
    monkeypatch.setattr(module, "output_isatty", lambda: False)
    monkeypatch.setattr(
        module,
        "emit_toon",
        lambda payload: (_ for _ in ()).throw(AssertionError("completed gate run should not emit final TOON")),
    )
    monkeypatch.setattr(sys, "argv", [f"{module.__name__}.py", "--base", "main"])

    exit_code = module.main()

    assert exit_code == 0
    assert captured["review_scope"]["base"] == "main"


@pytest.mark.parametrize("module", [review_t2, review_t4])
def test_gate_wrappers_keep_final_toon_for_blocked_noninteractive_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module,
) -> None:
    emitted: list[dict[str, object]] = []

    monkeypatch.setattr(module, "resolve_repo_root", lambda cd: tmp_path)
    monkeypatch.setattr(
        module,
        "build_local_review_request",
        lambda **kwargs: SimpleNamespace(review_scope={"base": "main"}, prompt=""),
    )
    monkeypatch.setattr(
        module,
        "run_gate_round",
        lambda **kwargs: ({"status": "blocked", "blocked": True, "runs": []}, 1),
    )
    monkeypatch.setattr(module, "guard_branch_signoff_lane", lambda **kwargs: None)
    monkeypatch.setattr(module, "output_isatty", lambda: False)
    monkeypatch.setattr(module, "emit_toon", lambda payload: emitted.append(payload))
    monkeypatch.setattr(sys, "argv", [f"{module.__name__}.py", "--base", "main"])

    exit_code = module.main()

    assert exit_code == 1
    assert emitted == [{"status": "blocked", "blocked": True, "runs": []}]
