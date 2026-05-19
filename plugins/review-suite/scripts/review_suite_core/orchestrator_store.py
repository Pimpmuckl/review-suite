from __future__ import annotations

import json
import os
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any


ORCHESTRATOR_INDEX_SCHEMA_VERSION = 1
STATE_DIR_LOCATOR_SCHEMA_VERSION = 1


def orchestrator_dir(state_dir: Path) -> Path:
    return state_dir / "orchestrator"


def cycles_dir(state_dir: Path) -> Path:
    return orchestrator_dir(state_dir) / "cycles"


def index_path(state_dir: Path) -> Path:
    return orchestrator_dir(state_dir) / "index.json"


def state_dir_locator_path(state_dir: Path) -> Path:
    return orchestrator_dir(state_dir) / "state_dirs.json"


def cycle_path(state_dir: Path, cycle_key: str) -> Path:
    return cycles_dir(state_dir) / f"{cycle_key}.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid orchestrator JSON at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"orchestrator JSON must be an object: {path}")
    return payload


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


def _empty_index() -> dict[str, Any]:
    return {
        "schema_version": ORCHESTRATOR_INDEX_SCHEMA_VERSION,
        "ids": {},
        "cycle_keys": {},
    }


def load_index(state_dir: Path) -> dict[str, Any]:
    raw = _read_json(index_path(state_dir))
    if not raw:
        return _empty_index()
    ids = raw.get("ids") if isinstance(raw.get("ids"), dict) else {}
    cycle_keys = raw.get("cycle_keys") if isinstance(raw.get("cycle_keys"), dict) else {}
    return {
        "schema_version": ORCHESTRATOR_INDEX_SCHEMA_VERSION,
        "ids": {str(key): str(value) for key, value in ids.items()},
        "cycle_keys": {str(key): str(value) for key, value in cycle_keys.items()},
    }


def _write_index(state_dir: Path, index: dict[str, Any]) -> None:
    _atomic_write_json(index_path(state_dir), index)


def _empty_state_dir_locator() -> dict[str, Any]:
    return {
        "schema_version": STATE_DIR_LOCATOR_SCHEMA_VERSION,
        "ids": {},
        "cycle_keys": {},
    }


def load_state_dir_locator(state_dir: Path) -> dict[str, Any]:
    raw = _read_json(state_dir_locator_path(state_dir))
    if not raw:
        return _empty_state_dir_locator()
    ids = raw.get("ids") if isinstance(raw.get("ids"), dict) else {}
    cycle_keys = raw.get("cycle_keys") if isinstance(raw.get("cycle_keys"), dict) else {}
    return {
        "schema_version": STATE_DIR_LOCATOR_SCHEMA_VERSION,
        "ids": {str(key): str(value) for key, value in ids.items()},
        "cycle_keys": {str(key): str(value) for key, value in cycle_keys.items()},
    }


def _write_state_dir_locator(state_dir: Path, locator: dict[str, Any]) -> None:
    _atomic_write_json(state_dir_locator_path(state_dir), locator)


def _normalized_state_dir(state_dir: Path) -> str:
    return str(state_dir.resolve(strict=False))


def register_cycle_state_dir(
    *,
    locator_state_dir: Path,
    state_dir: Path,
    public_id: str,
    cycle_key: str,
) -> None:
    review_id = str(public_id or "").strip()
    key = str(cycle_key or "").strip()
    if not review_id or not key:
        return
    locator = load_state_dir_locator(locator_state_dir)
    normalized = _normalized_state_dir(state_dir)
    locator["ids"][review_id] = normalized
    locator["cycle_keys"][key] = normalized
    _write_state_dir_locator(locator_state_dir, locator)


def state_dir_for_public_id(locator_state_dir: Path, public_id: str) -> Path | None:
    review_id = str(public_id or "").strip()
    if not review_id:
        return None
    locator = load_state_dir_locator(locator_state_dir)
    value = str(locator["ids"].get(review_id) or "").strip()
    return Path(value) if value else None


def _public_id_candidates(cycle_key: str) -> list[str]:
    digest = cycle_key.removeprefix("orc-")
    lengths = (8, 10, 12, 16, len(digest))
    return [f"rvw_{digest[:length]}" for length in lengths if length <= len(digest)]


def public_id_for_cycle_key(state_dir: Path, cycle_key: str) -> str:
    index = load_index(state_dir)
    existing = str(index["cycle_keys"].get(cycle_key) or "")
    if existing:
        if index["ids"].get(existing) != cycle_key:
            index["ids"][existing] = cycle_key
            _write_index(state_dir, index)
        return existing
    for candidate in _public_id_candidates(cycle_key):
        if candidate not in index["ids"]:
            index["ids"][candidate] = cycle_key
            index["cycle_keys"][cycle_key] = candidate
            _write_index(state_dir, index)
            return candidate
    raise ValueError(f"could not allocate public id for cycle key: {cycle_key}")


def load_cycle_by_key(state_dir: Path, cycle_key: str) -> dict[str, Any] | None:
    path = cycle_path(state_dir, cycle_key)
    if not path.exists():
        return None
    return _read_json(path)


def load_cycle_by_public_id(state_dir: Path, public_id: str) -> dict[str, Any]:
    review_id = str(public_id or "").strip()
    if not review_id:
        raise ValueError("--id is required")
    index = load_index(state_dir)
    cycle_key = str(index["ids"].get(review_id) or "")
    if not cycle_key:
        raise ValueError(f"unknown review cycle id: {review_id}")
    state = load_cycle_by_key(state_dir, cycle_key)
    if state is None:
        raise ValueError(f"review cycle state is missing for id: {review_id}")
    state.setdefault("public_id", review_id)
    return state


def save_cycle(state_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    cycle_key = str(state.get("cycle_key") or "").strip()
    if not cycle_key:
        raise ValueError("cycle_key is required")
    payload = deepcopy(state)
    payload["public_id"] = public_id_for_cycle_key(state_dir, cycle_key)
    _atomic_write_json(cycle_path(state_dir, cycle_key), payload)
    return payload
