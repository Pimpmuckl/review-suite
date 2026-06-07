#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from review_suite_runtime_bootstrap import bootstrap_from_installed_cache

bootstrap_from_installed_cache(__file__)

from review_gate import public_gate_completion_payload, run_gate_round
from review_suite_local import (
    build_local_review_request,
    build_phase_instructions,
    default_roster_path,
    default_state_dir,
    guard_branch_signoff_lane,
    guard_no_stage_step_down,
    load_custom_instructions,
    output_isatty,
    resolve_caller_id,
)
from review_suite_core import (
    AxiArgumentParser,
    DEFAULT_PROGRESS_INTERVAL_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    emit_error,
    emit_toon,
    format_command,
    resolve_repo_root,
)


def build_parser() -> argparse.ArgumentParser:
    parser = AxiArgumentParser(
        description="Tier 2 local gate review wrapper. Reviewer completion must be closed explicitly as clean or findings."
    )
    parser.add_argument("--cd")
    parser.add_argument("--task-id", help=argparse.SUPPRESS)
    parser.add_argument("--base", default="main")
    parser.add_argument("--commit", nargs="+")
    parser.add_argument("--instructions")
    parser.add_argument("--instructions-file")
    parser.add_argument("--roster", default=str(default_roster_path()), help=argparse.SUPPRESS)
    parser.add_argument("--state-dir", default=str(default_state_dir()), help=argparse.SUPPRESS)
    parser.add_argument("--sqlite-path", default=str(Path.home() / ".codex" / "state_5.sqlite"), help=argparse.SUPPRESS)
    parser.add_argument("--wsl", action="store_true")
    parser.add_argument("--champion-override")
    parser.add_argument("--allow-stage-step-down", action="store_true", help=argparse.SUPPRESS)
    return parser


def _help_command() -> str:
    return format_command([sys.executable, str(Path(__file__).resolve()), "--help"])


def main() -> int:
    parser = build_parser()
    try:
        args = parser.parse_args()
        caller_id, caller_id_source = resolve_caller_id(None)
        review_cwd = resolve_repo_root(args.cd)
        request = build_local_review_request(
            review_cwd=review_cwd,
            base=str(args.base),
            commit_values=args.commit,
            instruction_builder=build_phase_instructions,
            custom_instructions=load_custom_instructions(
                instructions=args.instructions,
                instructions_file=args.instructions_file,
            ),
        )
        if not bool(args.allow_stage_step_down):
            guard_no_stage_step_down(
                lane="review_t2",
                review_cwd=review_cwd,
                base=str(args.base),
                state_dir=Path(args.state_dir),
                review_scope=request.review_scope,
            )
        guard_branch_signoff_lane(
            lane="review_t2",
            review_cwd=review_cwd,
            base=str(args.base),
            state_dir=Path(args.state_dir),
            review_scope=request.review_scope,
        )
        payload, exit_code = run_gate_round(
            gate_task_class="phase_gate",
            review_cwd=review_cwd,
            roster_path=Path(args.roster),
            state_dir=Path(args.state_dir),
            sqlite_path=Path(args.sqlite_path),
            task_id=args.task_id,
            progress_interval_seconds=DEFAULT_PROGRESS_INTERVAL_SECONDS,
            timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
            allow_unsafe_windows_wsl_fallback=bool(args.wsl),
            review_scope=request.review_scope,
            prompt=request.prompt,
            champion_override=args.champion_override,
            caller_id=caller_id,
            caller_id_source=caller_id_source,
        )
        if str(payload.get("status") or "") == "signoff_pending" or (
            not output_isatty() and str(payload.get("status") or "") != "completed"
        ):
            emit_toon(public_gate_completion_payload(payload))
        return exit_code
    except ValueError as exc:
        return emit_error(
            str(exc),
            status="usage_error",
            help_items=[_help_command()],
        )


if __name__ == "__main__":
    raise SystemExit(main())
