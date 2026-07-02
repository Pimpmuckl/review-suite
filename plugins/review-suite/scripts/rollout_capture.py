from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_CODEX_HOME = Path.home() / ".codex"
DEFAULT_SQLITE_STATE_PATH = DEFAULT_CODEX_HOME / "state_5.sqlite"
REVIEW_SUBAGENT_SOURCE = json.dumps({"subagent": "review"}, separators=(",", ":"))

_JSONL_CACHE: dict[tuple[str, int, int], list[dict[str, Any]]] = {}
_ROLLOUT_SUMMARY_CACHE: dict[tuple[str, int, int], dict[str, Any]] = {}
_THREAD_LOOKUP_CACHE: dict[
    tuple[str, int, int, str, str | None, int | None], dict[str, Any] | None
] = {}


def _path_cache_key(path: Path) -> tuple[str, int, int]:
    stat = path.stat()
    return (str(path), stat.st_mtime_ns, stat.st_size)


def iter_jsonl(path: Path) -> list[dict[str, Any]]:
    key = _path_cache_key(path)
    cached = _JSONL_CACHE.get(key)
    if cached is not None:
        return cached
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    _JSONL_CACHE[key] = rows
    return rows


def _token_usage_dict(value: dict[str, Any] | None) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    return {
        "input_tokens": int(value.get("input_tokens", 0) or 0),
        "cached_input_tokens": int(value.get("cached_input_tokens", 0) or 0),
        "output_tokens": int(value.get("output_tokens", 0) or 0),
    }


def read_rollout_summary(path: Path) -> dict[str, Any]:
    key = _path_cache_key(path)
    cached = _ROLLOUT_SUMMARY_CACHE.get(key)
    if cached is not None:
        return cached
    rows = iter_jsonl(path)
    last_turn_context_index = 0
    for index, row in enumerate(rows):
        if row.get("type") == "turn_context":
            last_turn_context_index = index
    first_total: dict[str, int] | None = None
    first_last: dict[str, int] | None = None
    final_total: dict[str, int] | None = None
    final_text = ""
    for row in rows[last_turn_context_index:]:
        row_type = row.get("type")
        if row_type == "event_msg":
            payload = row.get("payload", {})
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                continue
            info = payload.get("info", {})
            if not isinstance(info, dict):
                continue
            total_usage = _token_usage_dict(info.get("total_token_usage"))
            last_usage = _token_usage_dict(info.get("last_token_usage"))
            if total_usage is None:
                continue
            if first_total is None:
                first_total = total_usage
                first_last = last_usage or {
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "output_tokens": 0,
                }
            final_total = total_usage
            continue
        if row_type != "response_item":
            continue
        payload = row.get("payload", {})
        if not isinstance(payload, dict):
            continue
        if payload.get("type") != "message" or payload.get("role") != "assistant":
            continue
        content = payload.get("content", [])
        if not isinstance(content, list):
            continue
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("text"):
                parts.append(item["text"])
        if parts:
            final_text = "\n".join(parts).strip()
    usage: dict[str, int] | None = None
    if final_total is not None:
        if first_total is None:
            usage = final_total
        else:
            if first_last is None:
                first_last = {
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "output_tokens": 0,
                }
            usage = {
                metric: max(
                    0, final_total[metric] - first_total[metric] + first_last[metric]
                )
                for metric in ("input_tokens", "cached_input_tokens", "output_tokens")
            }
    summary = {"usage": usage, "reviewer_output": final_text}
    _ROLLOUT_SUMMARY_CACHE[key] = summary
    return summary


def rollout_final_token_usage(path: Path) -> dict[str, int] | None:
    return read_rollout_summary(path).get("usage")


def rollout_final_assistant_text(path: Path) -> str:
    return str(read_rollout_summary(path).get("reviewer_output") or "")


def _parse_event_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_meaningful_activity(row: dict[str, Any]) -> bool:
    row_type = row.get("type")
    payload = row.get("payload", {})
    if not isinstance(payload, dict):
        return False
    if row_type == "response_item":
        payload_type = str(payload.get("type") or "")
        if payload_type in {
            "custom_tool_call",
            "custom_tool_call_output",
            "function_call",
            "function_call_output",
            "reasoning",
        }:
            return True
        if payload_type != "message" or payload.get("role") != "assistant":
            return False
        content = payload.get("content", [])
        if not isinstance(content, list):
            return False
        return any(
            isinstance(item, dict)
            and str(item.get("text") or item.get("output_text") or "").strip()
            for item in content
        )
    if row_type == "event_msg":
        return str(payload.get("type") or "") in {"agent_message", "task_complete"}
    return False


def rollout_activity_summary(path: Path) -> dict[str, Any]:
    rows = iter_jsonl(path)
    last_turn_context_index = 0
    for index, row in enumerate(rows):
        if row.get("type") == "turn_context":
            last_turn_context_index = index
    last_event_at: datetime | None = None
    last_meaningful_at: datetime | None = None
    last_meaningful_type: str | None = None
    for row in rows[last_turn_context_index:]:
        timestamp = _parse_event_timestamp(row.get("timestamp"))
        if timestamp is not None:
            last_event_at = timestamp
        if _is_meaningful_activity(row):
            last_meaningful_at = timestamp
            payload = row.get("payload", {})
            last_meaningful_type = str(payload.get("type") or row.get("type") or "")
    return {
        "last_event_at": last_event_at,
        "last_meaningful_at": last_meaningful_at,
        "last_meaningful_type": last_meaningful_type,
    }


def _coerce_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def _missing_threads_table(exc: sqlite3.Error) -> bool:
    return "no such table: threads" in str(exc).lower()


def find_thread_by_title(
    *,
    sqlite_path: Path,
    title: str,
    cwd: str | None = None,
    created_after: int | None = None,
) -> dict[str, Any] | None:
    if not sqlite_path.is_file():
        return None
    stat = sqlite_path.stat()
    cache_key = (
        str(sqlite_path),
        stat.st_mtime_ns,
        stat.st_size,
        title,
        cwd,
        created_after,
    )
    cached = _THREAD_LOOKUP_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached) if cached else None
    con = sqlite3.connect(str(sqlite_path))
    con.row_factory = sqlite3.Row
    try:
        try:
            sql = """
                select id, rollout_path, cwd, source, created_at, updated_at, tokens_used, model, reasoning_effort, title
                from threads
                where title = ?
            """
            params: list[Any] = [title]
            if cwd is not None:
                sql += " and cwd = ?"
                params.append(cwd)
            if created_after is not None:
                sql += " and created_at >= ?"
                params.append(created_after)
            sql += " order by updated_at desc limit 1"
            row = con.execute(sql, params).fetchone()
        except sqlite3.OperationalError as exc:
            if not _missing_threads_table(exc):
                raise
            row = None
    finally:
        con.close()
    result = dict(row) if row else None
    _THREAD_LOOKUP_CACHE[cache_key] = result
    return dict(result) if result else None


def find_review_child_thread(
    *,
    sqlite_path: Path,
    cwd: str | None,
    model: str | None,
    reasoning_effort: str | None,
    title: str | None = None,
    created_at: int | None = None,
    updated_at: int | None = None,
    created_window_seconds: int = 5,
) -> dict[str, Any] | None:
    if not sqlite_path.is_file():
        return None
    cwd_value = str(cwd or "").strip()
    model_value = str(model or "").strip()
    effort_value = str(reasoning_effort or "").strip()
    title_value = str(title or "").strip()
    if not cwd_value or not model_value or not effort_value:
        return None
    created_at_value = _coerce_optional_int(created_at)
    updated_at_value = _coerce_optional_int(updated_at)
    window_seconds = max(int(created_window_seconds or 0), 1)
    created_after = None
    created_before = None
    if created_at_value is not None:
        created_after = max(created_at_value - 1, 0)
        created_before = created_at_value + window_seconds
        if updated_at_value is not None:
            created_before = max(created_before, updated_at_value)
    stat = sqlite_path.stat()
    cache_key = (
        str(sqlite_path),
        stat.st_mtime_ns,
        stat.st_size,
        f"review_child:{cwd_value}:{title_value or '-'}:{model_value.lower()}:{effort_value.lower()}:{created_at_value}:{updated_at_value}:{window_seconds}",
        None,
        None,
    )
    cached = _THREAD_LOOKUP_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached) if cached else None
    con = sqlite3.connect(str(sqlite_path))
    con.row_factory = sqlite3.Row
    try:
        try:
            sql = """
                select id, rollout_path, cwd, source, created_at, updated_at, tokens_used, model, reasoning_effort, title
                from threads
                where cwd = ?
                  and source = ?
                  and lower(coalesce(model, '')) = lower(?)
                  and lower(coalesce(reasoning_effort, '')) = lower(?)
            """
            params: list[Any] = [
                cwd_value,
                REVIEW_SUBAGENT_SOURCE,
                model_value,
                effort_value,
            ]
            if title_value:
                sql += " and title = ?"
                params.append(title_value)
            if created_after is not None and created_before is not None:
                sql += " and created_at between ? and ?"
                params.extend([created_after, created_before])
            order_parts: list[str] = []
            if updated_at_value is not None:
                order_parts.append("abs(updated_at - ?)")
                params.append(updated_at_value)
            if created_at_value is not None:
                order_parts.append("abs(created_at - ?)")
                params.append(created_at_value)
            order_parts.extend(["updated_at desc", "id asc"])
            sql += " order by " + ", ".join(order_parts) + " limit 1"
            row = con.execute(sql, params).fetchone()
        except sqlite3.OperationalError as exc:
            if not _missing_threads_table(exc):
                raise
            row = None
    finally:
        con.close()
    result = dict(row) if row else None
    _THREAD_LOOKUP_CACHE[cache_key] = result
    return dict(result) if result else None


def find_thread_by_id(*, sqlite_path: Path, thread_id: str) -> dict[str, Any] | None:
    if not sqlite_path.is_file():
        return None
    con = sqlite3.connect(str(sqlite_path))
    con.row_factory = sqlite3.Row
    try:
        try:
            row = con.execute(
                """
                select id, rollout_path, cwd, source, created_at, updated_at, tokens_used, model, reasoning_effort, title
                from threads
                where id = ?
                limit 1
                """,
                [thread_id],
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if not _missing_threads_table(exc):
                raise
            row = None
    finally:
        con.close()
    return dict(row) if row else None


def enrich_thread_record(thread_row: dict[str, Any]) -> dict[str, Any]:
    rollout_path = Path(thread_row["rollout_path"])
    summary = read_rollout_summary(rollout_path) if rollout_path.is_file() else {}
    enriched = dict(thread_row)
    enriched["usage"] = summary.get("usage") or {}
    enriched["reviewer_output"] = summary.get("reviewer_output") or ""
    return enriched
