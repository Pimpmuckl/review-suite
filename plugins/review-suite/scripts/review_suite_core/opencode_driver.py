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
REVIEW_METADATA_PREFIX = "[review-suite] opencode-metadata: "
REVIEW_EXPORT_TIMEOUT_SECONDS = 120


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
        raise RuntimeError(
            proc.stderr.strip() or "failed to produce review target diff"
        )
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


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except TypeError, ValueError:
        return 0


def _new_usage_totals() -> dict[str, Any]:
    return {
        "input": 0,
        "output": 0,
        "reasoning": 0,
        "cache_read": 0,
        "cache_write": 0,
        "total": 0,
        "cost": 0.0,
    }


def _add_usage_totals(totals: dict[str, Any], tokens: Any, cost: Any) -> bool:
    saw = False
    if isinstance(tokens, dict):
        cache = tokens.get("cache")
        if not isinstance(cache, dict):
            cache = {}
        totals["input"] += _int_value(tokens.get("input"))
        totals["output"] += _int_value(tokens.get("output"))
        totals["reasoning"] += _int_value(tokens.get("reasoning"))
        totals["cache_read"] += _int_value(cache.get("read"))
        totals["cache_write"] += _int_value(cache.get("write"))
        totals["total"] += _int_value(tokens.get("total"))
        saw = True
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        totals["cost"] += float(cost)
        saw = True
    return saw


def _usage_totals_to_usage(totals: dict[str, Any]) -> dict[str, int]:
    cache_read = int(totals["cache_read"])
    cache_write = int(totals["cache_write"])
    reasoning = int(totals["reasoning"])
    usage = {
        "input_tokens": int(totals["input"]) + cache_read + cache_write,
        "cached_input_tokens": cache_read,
        "output_tokens": int(totals["output"]),
    }
    if cache_write:
        usage["cache_write_tokens"] = cache_write
    if reasoning:
        usage["reasoning_output_tokens"] = reasoning
    if int(totals["total"]):
        usage["total_tokens"] = int(totals["total"])
    return usage


def _parse_event_stream(stdout: str) -> dict[str, Any]:
    session_id: str | None = None
    current_parts: list[str] = []
    completed_messages: list[str] = []
    totals = _new_usage_totals()
    saw_usage = False
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
        if event_type == "step_finish":
            part = event.get("part")
            if isinstance(part, dict) and _add_usage_totals(
                totals, part.get("tokens"), part.get("cost")
            ):
                saw_usage = True
            if current_parts:
                completed_messages.append("\n".join(current_parts).strip())
                current_parts = []
    if current_parts:
        completed_messages.append("\n".join(current_parts).strip())

    terminal_messages = [
        text
        for text in completed_messages
        if TERMINAL_REVIEW_RESULT_PREFIX.lower() in text.lower()
    ]
    return {
        "session_id": session_id,
        "reviewer_output": terminal_messages[-1] if terminal_messages else None,
        "usage": _usage_totals_to_usage(totals) if saw_usage else {},
        "cost_usd": round(totals["cost"], 9) if saw_usage else None,
    }


def _usage_from_export(payload: Any) -> tuple[dict[str, int], float | None]:
    if not isinstance(payload, dict):
        return {}, None
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return {}, None
    totals = _new_usage_totals()
    saw_usage = False
    for message in messages:
        info = message.get("info") if isinstance(message, dict) else None
        if not isinstance(info, dict) or str(info.get("role") or "") != "assistant":
            continue
        if _add_usage_totals(totals, info.get("tokens"), info.get("cost")):
            saw_usage = True
    if not saw_usage:
        return {}, None
    return _usage_totals_to_usage(totals), round(totals["cost"], 9)


def _format_metadata_line(
    session_id: str | None, usage: dict[str, int], cost_usd: float | None
) -> str:
    payload: dict[str, Any] = {"session_id": session_id, "usage": usage}
    if cost_usd is not None:
        payload["cost_usd"] = cost_usd
    return REVIEW_METADATA_PREFIX + json.dumps(payload, separators=(",", ":")) + "\n"


def parse_opencode_review_metadata(text: str) -> dict[str, Any]:
    for line in str(text or "").splitlines():
        marker = line.find(REVIEW_METADATA_PREFIX)
        if marker == -1:
            continue
        raw_payload = line[marker + len(REVIEW_METADATA_PREFIX) :].strip()
        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


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
        if (
            text
            and not (isinstance(info, dict) and info.get("summary"))
            and (
                TERMINAL_REVIEW_RESULT_PREFIX.lower() in text.lower()
                or (isinstance(info, dict) and info.get("finish") == "stop")
            )
        ):
            candidates.append(text)
    for nested in value.values():
        if isinstance(nested, dict | list):
            candidates.extend(_assistant_text_candidates(nested))
    return candidates


def _fetch_export_payload(
    opencode: str, session_id: str, review_root: Path
) -> dict[str, Any] | None:
    try:
        proc = subprocess.run(
            [opencode, "export", session_id],
            cwd=review_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=REVIEW_EXPORT_TIMEOUT_SECONDS,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _exported_review_text(
    opencode: str, session_id: str, review_root: Path
) -> str | None:
    payload = _fetch_export_payload(opencode, session_id, review_root)
    if payload is None:
        return None
    candidates = _assistant_text_candidates(payload)
    return candidates[-1] if candidates else None


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
        stream = _parse_event_stream(proc.stdout)
        session_id = stream["session_id"]
        reviewer_output = stream["reviewer_output"]
        usage = stream["usage"]
        cost_usd = stream["cost_usd"]
        export_payload = (
            _fetch_export_payload(opencode, session_id, review_root)
            if session_id
            else None
        )
        if not reviewer_output and export_payload is not None:
            candidates = _assistant_text_candidates(export_payload)
            reviewer_output = candidates[-1] if candidates else None
        export_usage, export_cost = _usage_from_export(export_payload)
        if export_usage:
            usage = export_usage
        if export_cost is not None:
            cost_usd = export_cost
        if session_id or usage:
            sys.stderr.write(_format_metadata_line(session_id, usage, cost_usd))
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
