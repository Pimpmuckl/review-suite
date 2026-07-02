from __future__ import annotations

import argparse
import math
import os
import re
import shlex
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any


_INDENT = "  "
_SAFE_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_NUMBER_LIKE_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$")


class AxiArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def emit_toon(value: Any) -> None:
    rendered = toon_dumps(value)
    write_text(rendered)


def write_text(value: Any, *, stream: Any | None = None, end: str = "\n") -> None:
    target = stream if stream is not None else sys.stdout
    rendered = f"{value}{end}"
    try:
        target.write(rendered)
    except UnicodeEncodeError:
        encoding = getattr(target, "encoding", None) or "utf-8"
        buffer = getattr(target, "buffer", None)
        if buffer is not None:
            buffer.write(rendered.encode(encoding, errors="backslashreplace"))
        else:
            target.write(
                rendered.encode("ascii", errors="backslashreplace").decode("ascii")
            )
    flush = getattr(target, "flush", None)
    if callable(flush):
        flush()


def format_command(parts: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(parts)
    return shlex.join(parts)


def emit_error(
    message: str,
    *,
    status: str = "error",
    help_items: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> int:
    payload: dict[str, Any] = {
        "status": status,
        "error": message,
    }
    if extra:
        for key, value in extra.items():
            if value is None:
                continue
            if value == [] or value == {}:
                continue
            payload[key] = value
    if help_items:
        payload["help"] = help_items
    emit_toon(payload)
    return 2 if status == "usage_error" else 1


def toon_dumps(value: Any) -> str:
    normalized = _normalize(value)
    lines: list[str] = []
    if isinstance(normalized, dict):
        for key, child in normalized.items():
            _emit_field(lines, 0, str(key), child)
    elif isinstance(normalized, list):
        _emit_array(lines, 0, None, normalized)
    else:
        lines.append(_encode_primitive(normalized, delimiter=","))
    return "\n".join(lines)


def _normalize(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _normalize(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(child) for child in value]
    return str(value)


def _emit_field(lines: list[str], depth: int, key: str, value: Any) -> None:
    indent = _INDENT * depth
    encoded_key = _encode_key(key)
    if _is_primitive(value):
        lines.append(
            f"{indent}{encoded_key}: {_encode_primitive(value, delimiter=',')}"
        )
        return
    if isinstance(value, dict):
        lines.append(f"{indent}{encoded_key}:")
        for child_key, child_value in value.items():
            _emit_field(lines, depth + 1, str(child_key), child_value)
        return
    if isinstance(value, list):
        _emit_array(lines, depth, encoded_key, value)
        return
    lines.append(
        f"{indent}{encoded_key}: {_encode_primitive(str(value), delimiter=',')}"
    )


def _emit_array(
    lines: list[str],
    depth: int,
    key: str | None,
    values: list[Any],
    *,
    list_item: bool = False,
) -> None:
    indent = _INDENT * depth
    prefix = "- " if list_item else ""
    header_start = f"{prefix}{key}" if key else prefix.rstrip()
    if _all_primitives(values):
        header = (
            f"{header_start}[{len(values)}]:" if header_start else f"[{len(values)}]:"
        )
        encoded_values = ",".join(
            _encode_primitive(value, delimiter=",") for value in values
        )
        line = f"{indent}{header}"
        if encoded_values:
            line += f" {encoded_values}"
        lines.append(line)
        return
    fields = _tabular_fields(values)
    if fields:
        fields_text = ",".join(_encode_key(field) for field in fields)
        header = (
            f"{header_start}[{len(values)}]{{{fields_text}}}:"
            if header_start
            else f"[{len(values)}]{{{fields_text}}}:"
        )
        lines.append(f"{indent}{header}")
        child_indent = _INDENT * (depth + (2 if list_item else 1))
        for row in values:
            assert isinstance(row, dict)
            encoded_row = ",".join(
                _encode_primitive(row.get(field), delimiter=",") for field in fields
            )
            lines.append(f"{child_indent}{encoded_row}")
        return
    header = f"{header_start}[{len(values)}]:" if header_start else f"[{len(values)}]:"
    lines.append(f"{indent}{header}")
    item_depth = depth + 1
    for item in values:
        _emit_list_item(lines, item_depth, item)


def _emit_list_item(lines: list[str], depth: int, value: Any) -> None:
    indent = _INDENT * depth
    if _is_primitive(value):
        lines.append(f"{indent}- {_encode_primitive(value, delimiter=',')}")
        return
    if isinstance(value, list):
        _emit_array(lines, depth, None, value, list_item=True)
        return
    if isinstance(value, dict):
        items = list(value.items())
        if not items:
            lines.append(f"{indent}-")
            return
        first_key, first_value = items[0]
        encoded_first_key = _encode_key(str(first_key))
        if _is_primitive(first_value):
            lines.append(
                f"{indent}- {encoded_first_key}: {_encode_primitive(first_value, delimiter=',')}"
            )
        elif isinstance(first_value, list):
            _emit_array(lines, depth, encoded_first_key, first_value, list_item=True)
        else:
            lines.append(f"{indent}- {encoded_first_key}:")
            for child_key, child_value in dict(first_value).items():
                _emit_field(lines, depth + 2, str(child_key), child_value)
        for child_key, child_value in items[1:]:
            _emit_field(lines, depth + 1, str(child_key), child_value)
        return
    lines.append(f"{indent}- {_encode_primitive(str(value), delimiter=',')}")


def _tabular_fields(values: list[Any]) -> list[str] | None:
    if not values or not all(isinstance(item, dict) for item in values):
        return None
    first = values[0]
    assert isinstance(first, dict)
    fields = [str(key) for key in first.keys()]
    if not fields:
        return None
    field_set = set(fields)
    for item in values:
        assert isinstance(item, dict)
        if set(str(key) for key in item.keys()) != field_set:
            return None
        for field in fields:
            if not _is_primitive(item.get(field)):
                return None
    return fields


def _all_primitives(values: list[Any]) -> bool:
    return all(_is_primitive(value) for value in values)


def _is_primitive(value: Any) -> bool:
    return value is None or isinstance(value, (bool, int, float, str))


def _encode_key(value: str) -> str:
    if _SAFE_KEY_RE.fullmatch(value):
        return value
    return _quote_string(value)


def _encode_primitive(value: Any, *, delimiter: str) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return "null"
        rendered = format(Decimal(str(value)), "f").rstrip("0").rstrip(".")
        if not rendered:
            rendered = "0"
        if rendered == "-0":
            rendered = "0"
        return rendered
    if isinstance(value, str):
        if _needs_quotes(value, delimiter=delimiter):
            return _quote_string(value)
        return value
    return _quote_string(str(value))


def _needs_quotes(value: str, *, delimiter: str) -> bool:
    if value == "":
        return True
    if value != value.strip():
        return True
    if value in {"true", "false", "null"}:
        return True
    if _NUMBER_LIKE_RE.fullmatch(value):
        return True
    if value == "-" or value.startswith("-"):
        return True
    if any(
        character in value
        for character in (":", '"', "\\", "[", "]", "{", "}", delimiter)
    ):
        return True
    if any(ord(character) < 32 for character in value):
        return True
    return False


def _quote_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'
