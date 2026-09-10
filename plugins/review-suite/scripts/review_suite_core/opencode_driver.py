from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


TERMINAL_REVIEW_RESULT_PREFIX = "Review result:"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one isolated OpenCode review.")
    parser.add_argument("--model", required=True)
    parser.add_argument("--dir", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--base")
    parser.add_argument("--commit")
    parser.add_argument("--commit-end")
    return parser


def _git_diff_command(args: argparse.Namespace) -> list[str]:
    common = ["git"]
    if args.commit_end:
        if not args.base or args.commit:
            raise ValueError("commit range requires --base and --commit-end")
        return [
            *common,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            f"{args.base}..{args.commit_end}",
        ]
    if bool(args.base) == bool(args.commit):
        raise ValueError("review requires exactly one of --base or --commit")
    if args.base:
        return [
            *common,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            f"{args.base}...HEAD",
        ]
    return [
        *common,
        "show",
        "--first-parent",
        "--format=fuller",
        "--no-ext-diff",
        "--no-textconv",
        str(args.commit),
    ]


def _write_target_patch(args: argparse.Namespace, review_root: Path) -> Path:
    proc = subprocess.run(
        _git_diff_command(args),
        cwd=review_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "failed to produce review target diff")
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="review-suite-opencode-target-",
        suffix=".patch",
        delete=False,
    )
    try:
        handle.write(proc.stdout)
        return Path(handle.name)
    finally:
        handle.close()


def _parse_event_stream(stdout: str) -> tuple[str | None, str | None]:
    session_id: str | None = None
    current_parts: list[str] = []
    completed_messages: list[str] = []
    for raw_line in stdout.splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        candidate_session = str(event.get("sessionID") or "").strip()
        if candidate_session:
            session_id = candidate_session
        event_type = str(event.get("type") or "")
        if event_type == "step_start":
            current_parts = []
            continue
        if event_type == "text":
            part = event.get("part")
            text = str(part.get("text") or "") if isinstance(part, dict) else ""
            if text:
                current_parts.append(text)
            continue
        if event_type == "step_finish" and current_parts:
            completed_messages.append("\n".join(current_parts).strip())
            current_parts = []
    if current_parts:
        completed_messages.append("\n".join(current_parts).strip())

    terminal_messages = [
        text
        for text in completed_messages
        if TERMINAL_REVIEW_RESULT_PREFIX.lower() in text.lower()
    ]
    return session_id, terminal_messages[-1] if terminal_messages else None


def _assistant_text_candidates(value: Any) -> list[str]:
    candidates: list[str] = []
    if isinstance(value, list):
        for item in value:
            candidates.extend(_assistant_text_candidates(item))
        return candidates
    if not isinstance(value, dict):
        return candidates

    role = str(value.get("role") or "").lower()
    info = value.get("info")
    if isinstance(info, dict):
        role = str(info.get("role") or role).lower()
    parts = value.get("parts")
    if role == "assistant" and isinstance(parts, list):
        text = "\n".join(
            str(part.get("text") or "")
            for part in parts
            if isinstance(part, dict)
            and str(part.get("type") or "") == "text"
            and str(part.get("text") or "")
        ).strip()
        if text:
            candidates.append(text)
    for nested in value.values():
        if isinstance(nested, dict | list):
            candidates.extend(_assistant_text_candidates(nested))
    return candidates


def _exported_review_text(
    opencode: str, session_id: str, review_root: Path
) -> str | None:
    proc = subprocess.run(
        [opencode, "export", session_id],
        cwd=review_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    candidates = _assistant_text_candidates(payload)
    terminal = [
        text
        for text in candidates
        if TERMINAL_REVIEW_RESULT_PREFIX.lower() in text.lower()
    ]
    return terminal[-1] if terminal else None


def _truncate(value: str, limit: int = 4000) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...[truncated]..."


def main() -> int:
    args = build_parser().parse_args()
    review_root = Path(args.dir).resolve()
    prompt = sys.stdin.read()
    if not prompt.strip():
        print("OpenCode review prompt is empty", file=sys.stderr)
        return 2

    opencode = (
        shutil.which("opencode")
        or shutil.which("opencode.exe")
        or shutil.which("opencode.cmd")
    )
    if not opencode:
        print("OpenCode CLI was not found on PATH", file=sys.stderr)
        return 127

    patch_path: Path | None = None
    try:
        patch_path = _write_target_patch(args, review_root)
        command = [
            opencode,
            "--pure",
            "run",
            "--format",
            "json",
            "--model",
            args.model,
            "--agent",
            "review-suite",
            "--dir",
            str(review_root),
            "--title",
            args.title,
            "--file",
            str(patch_path),
        ]
        proc = subprocess.run(
            command,
            cwd=review_root,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if proc.stderr:
            sys.stderr.write(proc.stderr)
            if not proc.stderr.endswith("\n"):
                sys.stderr.write("\n")
        session_id, reviewer_output = _parse_event_stream(proc.stdout)
        if not reviewer_output and session_id:
            reviewer_output = _exported_review_text(opencode, session_id, review_root)
        if proc.returncode != 0:
            if proc.stdout:
                print(f"[opencode stdout]\n{_truncate(proc.stdout)}", file=sys.stderr)
            return proc.returncode
        if not reviewer_output:
            if proc.stdout:
                print(f"[opencode stdout]\n{_truncate(proc.stdout)}", file=sys.stderr)
            print(
                "OpenCode completed without a usable reviewer response", file=sys.stderr
            )
            return 2
        sys.stdout.write(reviewer_output.strip() + "\n")
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"OpenCode review adapter failed: {exc}", file=sys.stderr)
        return 2
    finally:
        if patch_path is not None:
            try:
                patch_path.unlink(missing_ok=True)
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
