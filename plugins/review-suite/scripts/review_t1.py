#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from review_suite_runtime_bootstrap import bootstrap_from_installed_cache

bootstrap_from_installed_cache(__file__)

from review_suite_arena import BlockingRoundError, run_benchmarked_round
from review_suite_local import (
    build_local_review_request,
    build_phase_instructions,
    default_roster_path,
    default_rubric_path,
    default_state_dir,
    load_custom_instructions,
    resolve_caller_id,
)
from review_suite_core import AxiArgumentParser, DEFAULT_PROGRESS_INTERVAL_SECONDS, emit_error, format_command, resolve_repo_root


def build_parser() -> argparse.ArgumentParser:
    parser = AxiArgumentParser(
        description="Tier 1 local arena review wrapper.",
        epilog=(
            "If another arena round blocks this wrapper, the error names the exact "
            "round id and prints the next Action. Run that Action before starting another round."
        ),
    )
    parser.add_argument("--cd")
    parser.add_argument("--task-id", help=argparse.SUPPRESS)
    parser.add_argument("--base", default="main")
    parser.add_argument("--commit", nargs="+")
    parser.add_argument("--instructions")
    parser.add_argument("--instructions-file")
    parser.add_argument("--seed", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--roster", default=str(default_roster_path()), help=argparse.SUPPRESS)
    parser.add_argument("--rubric", default=str(default_rubric_path()), help=argparse.SUPPRESS)
    parser.add_argument("--state-dir", default=str(default_state_dir()), help=argparse.SUPPRESS)
    parser.add_argument("--sqlite-path", default=str(Path.home() / ".codex" / "state_5.sqlite"), help=argparse.SUPPRESS)
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--wsl", action="store_true")
    parser.add_argument("--caller-id", help=argparse.SUPPRESS)
    parser.add_argument("--allow-stage-step-down", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--ignore-pending-grades", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--winner", help=argparse.SUPPRESS)
    parser.add_argument("--basis", help=argparse.SUPPRESS)
    parser.add_argument("--note", help=argparse.SUPPRESS)
    parser.add_argument("--alpha-note", help=argparse.SUPPRESS)
    parser.add_argument("--bravo-note", help=argparse.SUPPRESS)
    return parser


def _help_command() -> str:
    return format_command([sys.executable, str(Path(__file__).resolve()), "--help"])


def main() -> int:
    parser = build_parser()
    try:
        args = parser.parse_args()
        review_cwd = resolve_repo_root(args.cd)
        caller_id, caller_id_source = resolve_caller_id(args.caller_id)
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
        return run_benchmarked_round(
            task_class="phase_review",
            review_cwd=review_cwd,
            roster_path=Path(args.roster),
            rubric_path=Path(args.rubric),
            state_dir=Path(args.state_dir),
            sqlite_path=Path(args.sqlite_path),
            seed=args.seed,
            allow_dirty=bool(args.allow_dirty),
            progress_interval_seconds=DEFAULT_PROGRESS_INTERVAL_SECONDS,
            allow_unsafe_windows_wsl_fallback=bool(args.wsl),
            review_scope=request.review_scope,
            prompt=request.prompt,
            caller_id=caller_id,
            caller_id_source=caller_id_source,
            ignore_pending_grades=bool(args.ignore_pending_grades),
            task_id=args.task_id,
            winner=args.winner,
            basis=args.basis,
            note=args.note,
            alpha_note=args.alpha_note,
            bravo_note=args.bravo_note,
            public_task_name="review_t1",
            allow_stage_step_down=bool(args.allow_stage_step_down),
        )
    except BlockingRoundError as exc:
        return emit_error(
            str(exc),
            status="usage_error",
            extra={"Action": exc.action_payload},
            help_items=[_help_command()],
        )
    except ValueError as exc:
        return emit_error(
            str(exc),
            status="usage_error",
            help_items=[_help_command()],
        )


if __name__ == "__main__":
    raise SystemExit(main())
