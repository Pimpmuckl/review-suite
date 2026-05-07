#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from review_suite_core import AxiArgumentParser, emit_error, emit_toon, format_command, utc_now_iso

ALLOWED_STATE_ITEMS = (
    "runs.jsonl",
    "summary.json",
    "leaderboard.md",
    "leaderboard_legacy.md",
    "operational_state.json",
    "rounds",
    "gate_runs.jsonl",
    "gate_summary.json",
    "gate_leaderboard.md",
    "gate_partials",
)


def build_parser() -> argparse.ArgumentParser:
    parser = AxiArgumentParser(description="One-shot migration from legacy review-rumble state into the canonical review-suite state root.")
    parser.add_argument("--source-dir", default=str(Path.home() / ".codex" / "state" / "review-rumble"))
    parser.add_argument("--target-dir", default=str(Path.home() / ".codex" / "state" / "review-suite"))
    parser.add_argument("--archive-suffix")
    parser.add_argument("--apply", action="store_true")
    return parser


def _help_command() -> str:
    return format_command([sys.executable, str(Path(__file__).resolve()), "--help"])


def _default_archive_suffix() -> str:
    return ".migrated-" + utc_now_iso().replace(":", "").replace("-", "")


def _allowed_existing_items(source_dir: Path) -> list[Path]:
    return [source_dir / name for name in ALLOWED_STATE_ITEMS if (source_dir / name).exists()]


def _copy_item(source: Path, target: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, target)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _relative_names(paths: list[Path], source_dir: Path) -> list[str]:
    return [str(path.relative_to(source_dir)).replace("\\", "/") for path in paths]


def _legacy_lock_entries(source_dir: Path) -> list[Path]:
    lock_dir = source_dir / ".locks"
    if not lock_dir.exists() or not lock_dir.is_dir():
        return []
    return [path for path in lock_dir.iterdir()]


def _dirs_overlap(left: Path, right: Path) -> bool:
    try:
        return left == right or left.is_relative_to(right) or right.is_relative_to(left)
    except ValueError:
        return False


def main() -> int:
    parser = build_parser()
    try:
        args = parser.parse_args()
        source_dir = Path(args.source_dir).expanduser().resolve()
        target_dir = Path(args.target_dir).expanduser().resolve()
        if not source_dir.exists():
            raise ValueError(f"legacy review-rumble state root does not exist: {source_dir}")
        if _dirs_overlap(source_dir, target_dir):
            raise ValueError(f"source and target review state roots must not overlap: {source_dir} <-> {target_dir}")
        items = _allowed_existing_items(source_dir)
        if not items:
            raise ValueError(f"no migratable review-rumble state artifacts found in: {source_dir}")
        archive_suffix = str(args.archive_suffix or _default_archive_suffix()).strip()
        if not archive_suffix:
            raise ValueError("--archive-suffix must not be empty")
        archive_dir = source_dir.with_name(source_dir.name + archive_suffix)
        if archive_dir.exists():
            raise ValueError(f"archive target already exists: {archive_dir}")
        existing_target_items = list(target_dir.iterdir()) if target_dir.exists() else []
        if existing_target_items:
            raise ValueError(f"target review-suite state root is not empty: {target_dir}")
        payload = {
            "status": "dry_run",
            "source_dir": str(source_dir),
            "target_dir": str(target_dir),
            "archive_dir": str(archive_dir),
            "copied_items": _relative_names(items, source_dir),
            "notes": [
                "Only the active benchmark and gate state surface is carried forward.",
                "Legacy extras remain only in the archived review-rumble folder after apply.",
                "Lock directories are not migrated; the new runtime recreates them on demand.",
            ],
        }
        if not args.apply:
            emit_toon(payload)
            return 0
        active_locks = _legacy_lock_entries(source_dir)
        if active_locks:
            raise ValueError(
                f"legacy review-rumble state root still has active lock entries; stop live writers first: {', '.join(path.name for path in active_locks)}"
            )
        target_dir.mkdir(parents=True, exist_ok=True)
        for item in items:
            _copy_item(item, target_dir / item.name)
        shutil.move(str(source_dir), str(archive_dir))
        payload["status"] = "migrated"
        emit_toon(payload)
        return 0
    except ValueError as exc:
        return emit_error(
            str(exc),
            status="usage_error",
            help_items=[_help_command()],
        )


if __name__ == "__main__":
    raise SystemExit(main())
