#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from review_suite_runtime_bootstrap import bootstrap_from_installed_cache

bootstrap_from_installed_cache(__file__)

from review_suite_core import (
    AxiArgumentParser,
    DEFAULT_PROGRESS_INTERVAL_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    emit_error,
    emit_result,
    format_command,
    lens_model_config,
    resolve_repo_root,
    run_codex,
    use_unsafe_windows_wsl_fallback,
    write_text,
)


def build_parser() -> argparse.ArgumentParser:
    parser = AxiArgumentParser(description="Review Deslop post-implementation review wrapper.")
    parser.add_argument("--cd")
    parser.add_argument("--base", default="main")
    parser.add_argument("--commit", nargs="+")
    parser.add_argument("--focus")
    parser.add_argument("--wsl", action="store_true")
    parser.add_argument("--output-only", action="store_true", help=argparse.SUPPRESS)
    return parser


def _help_command() -> str:
    return format_command([sys.executable, str(Path(__file__).resolve()), "--help"])


def normalize_commit_spec(commit_values: list[str] | None) -> tuple[str | None, str | None]:
    if not commit_values:
        return None, None
    if len(commit_values) == 1:
        return commit_values[0], None
    if len(commit_values) == 2:
        return commit_values[0], commit_values[1]
    raise ValueError("--commit accepts one sha or two shas")


def build_prompt(*, base: str | None, commit: str | None, commit_end: str | None, focus: str | None) -> str:
    focus_block = f"\nPay extra attention to this focus area:\n- {focus.strip()}\n" if focus else ""
    if commit and commit_end:
        target_block = (
            f"Review the commit range `{commit}..{commit_end}` in the current repository.\n"
            "Inspect that range and adjacent touched paths, not the whole branch diff.\n\n"
        )
    elif commit:
        target_block = (
            f"Review commit `{commit}` in the current repository.\n"
            "Inspect that commit and adjacent touched paths, not the whole branch diff.\n\n"
        )
    else:
        target_block = f"Review the current repository changes against base branch `{base}`.\n\n"
    return (
        target_block
        + "Prefer the smallest correct shape.\n\n"
        + "Inspect for:\n"
        + "- redundant code\n"
        + "- duplicated logic\n"
        + "- dead or unused code\n"
        + "- places where a smaller or more direct implementation would work\n"
        + "- unnecessary helpers, wrappers, flags, branching, or abstraction layers\n"
        + "- overcomplicated abstractions that can be collapsed\n"
        + focus_block
        + "\nReturn only concrete findings with severity, file path, and fix suggestion.\n"
        + "Skip style-only comments."
    )


def _result_returncode(result: dict[str, object]) -> int:
    value = result.get("returncode")
    if value is None:
        return 1
    return int(value)


def emit_output_only(*, tool_name: str, result: dict[str, object]) -> int:
    returncode = _result_returncode(result)
    body = str(result.get("final_message") or "").strip()
    if not body and returncode != 0:
        body = f"{tool_name} run timed out" if result.get("timed_out") else f"{tool_name} run failed"
    if body:
        write_text(body)
    return returncode


def main() -> int:
    parser = build_parser()
    try:
        args = parser.parse_args()
        commit, commit_end = normalize_commit_spec(args.commit)
        if commit and args.base != "main":
            raise ValueError("use either --base or --commit")
        review_root = resolve_repo_root(args.cd)
        if use_unsafe_windows_wsl_fallback(review_root, bool(args.wsl)):
            print(
                "[review-deslop] WARNING: using Windows Codex fallback for a WSL UNC repo. This bypasses the Codex sandbox and is not the happy path.",
                file=sys.stderr,
                flush=True,
            )
        model_config = lens_model_config("review-deslop")
        result = run_codex(
            tool_name="review-deslop",
            prompt=build_prompt(
                base=None if commit else args.base,
                commit=commit,
                commit_end=commit_end,
                focus=args.focus,
            ),
            model=model_config.model,
            reasoning_effort=model_config.reasoning_effort,
            service_tier=model_config.service_tier,
            review_root=review_root,
            progress_interval_seconds=DEFAULT_PROGRESS_INTERVAL_SECONDS,
            timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
            allow_unsafe_windows_wsl_fallback=bool(args.wsl),
        )
        if args.output_only:
            return emit_output_only(tool_name="review-deslop", result=result)
        return emit_result(
            tool_name="review-deslop",
            result=result,
        )
    except ValueError as exc:
        return emit_error(
            str(exc),
            status="usage_error",
            help_items=[_help_command()],
        )


if __name__ == "__main__":
    raise SystemExit(main())
