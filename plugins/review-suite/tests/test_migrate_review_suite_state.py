from __future__ import annotations

import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import migrate_review_suite_state


def test_migrate_review_suite_state_dry_run_only_lists_allowed_items(monkeypatch, tmp_path: Path) -> None:
    source_dir = tmp_path / "review-rumble"
    target_dir = tmp_path / "review-suite"
    source_dir.mkdir()
    (source_dir / "runs.jsonl").write_text("{}", encoding="utf-8")
    (source_dir / "operational_state.json").write_text("{}", encoding="utf-8")
    (source_dir / "junk.txt").write_text("junk", encoding="utf-8")
    emitted: list[dict[str, object]] = []

    monkeypatch.setattr(migrate_review_suite_state, "emit_toon", lambda payload: emitted.append(payload))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "migrate_review_suite_state.py",
            "--source-dir",
            str(source_dir),
            "--target-dir",
            str(target_dir),
        ],
    )

    exit_code = migrate_review_suite_state.main()

    assert exit_code == 0
    assert emitted[0]["status"] == "dry_run"
    assert emitted[0]["copied_items"] == ["runs.jsonl", "operational_state.json"]


def test_migrate_review_suite_state_apply_copies_allowed_items_and_archives_source(monkeypatch, tmp_path: Path) -> None:
    source_dir = tmp_path / "review-rumble"
    target_dir = tmp_path / "review-suite"
    archive_dir = tmp_path / "review-rumble.migrated-test"
    source_dir.mkdir()
    (source_dir / "runs.jsonl").write_text("{}", encoding="utf-8")
    (source_dir / "operational_state.json").write_text("{}", encoding="utf-8")
    rounds_dir = source_dir / "rounds"
    rounds_dir.mkdir()
    (rounds_dir / "round-1.json").write_text("{}", encoding="utf-8")
    (source_dir / ".locks").mkdir()
    (source_dir / "junk.txt").write_text("junk", encoding="utf-8")
    emitted: list[dict[str, object]] = []

    monkeypatch.setattr(migrate_review_suite_state, "emit_toon", lambda payload: emitted.append(payload))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "migrate_review_suite_state.py",
            "--source-dir",
            str(source_dir),
            "--target-dir",
            str(target_dir),
            "--archive-suffix",
            ".migrated-test",
            "--apply",
        ],
    )

    exit_code = migrate_review_suite_state.main()

    assert exit_code == 0
    assert emitted[0]["status"] == "migrated"
    assert (target_dir / "runs.jsonl").exists()
    assert (target_dir / "operational_state.json").exists()
    assert (target_dir / "rounds" / "round-1.json").exists()
    assert not (target_dir / ".locks").exists()
    assert not (target_dir / "junk.txt").exists()
    assert archive_dir.exists()
    assert (archive_dir / ".locks").exists()
    assert (archive_dir / "junk.txt").exists()
    assert not source_dir.exists()


def test_migrate_review_suite_state_rejects_overlapping_source_and_target_dirs(monkeypatch, tmp_path: Path) -> None:
    source_dir = tmp_path / "review-rumble"
    target_dir = source_dir / "nested-target"
    source_dir.mkdir(parents=True)
    (source_dir / "runs.jsonl").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "migrate_review_suite_state.py",
            "--source-dir",
            str(source_dir),
            "--target-dir",
            str(target_dir),
        ],
    )

    exit_code = migrate_review_suite_state.main()

    assert exit_code == 2


def test_migrate_review_suite_state_rejects_apply_when_legacy_locks_exist(monkeypatch, tmp_path: Path) -> None:
    source_dir = tmp_path / "review-rumble"
    target_dir = tmp_path / "review-suite"
    source_dir.mkdir()
    (source_dir / "runs.jsonl").write_text("{}", encoding="utf-8")
    locks_dir = source_dir / ".locks"
    locks_dir.mkdir()
    (locks_dir / "arena.lock").write_text("busy", encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "migrate_review_suite_state.py",
            "--source-dir",
            str(source_dir),
            "--target-dir",
            str(target_dir),
            "--apply",
        ],
    )

    exit_code = migrate_review_suite_state.main()

    assert exit_code == 2
