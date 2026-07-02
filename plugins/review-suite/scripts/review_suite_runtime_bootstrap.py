from __future__ import annotations

import atexit
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

PLUGIN_NAME = "review-suite"
DISABLE_ENV = "REVIEW_SUITE_DISABLE_RUNTIME_BOOTSTRAP"
LAUNCHER_SCRIPT_ENV = "REVIEW_SUITE_LAUNCHER_SCRIPT"
METADATA_FILENAME = "runtime_metadata.json"
RUNTIME_ITEMS = ("scripts", "references", "assets", "config", ".codex-plugin")
EXCLUDED_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}
RUNTIME_RETIRE_AFTER_SECONDS = (
    24 * 60 * 60
)  # ponytail: age guard; add process leases if multi-day runtime reuse matters.
RUNTIME_LEASE_STALE_AFTER_SECONDS = 10 * 60
RUNTIME_LEASE_REFRESH_SECONDS = 60
_RUNTIME_LEASES: dict[str, tuple[threading.Event, Path]] = {}


@dataclass(frozen=True)
class RuntimeBootstrapPlan:
    source_root: Path
    source_script: Path
    runtime_root: Path
    runtime_script: Path
    executable: str
    argv: tuple[str, ...]


def bootstrap_from_installed_cache(
    script_file: str | os.PathLike[str],
    *,
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    executable: str | None = None,
    execv: Callable[[str, Sequence[str]], object] = os.execv,
    run: Callable[[str, Sequence[str]], int] | None = None,
    platform_name: str | None = None,
) -> bool:
    plan = prepare_runtime_bootstrap(
        script_file,
        argv=argv,
        environ=environ,
        executable=executable,
    )
    if plan is None:
        _activate_runtime_lease(
            Path(script_file), os.environ if environ is None else environ
        )
        return False
    previous_launcher = os.environ.get(LAUNCHER_SCRIPT_ENV)
    os.environ[LAUNCHER_SCRIPT_ENV] = str(plan.source_script)
    try:
        if (platform_name or os.name) == "nt":
            exit_code = (
                run(plan.executable, plan.argv)
                if run
                else _run_runtime_process(plan.executable, plan.argv)
            )
            raise SystemExit(exit_code)
        execv(plan.executable, plan.argv)
        raise RuntimeError("runtime bootstrap exec returned unexpectedly")
    finally:
        if previous_launcher is None:
            os.environ.pop(LAUNCHER_SCRIPT_ENV, None)
        else:
            os.environ[LAUNCHER_SCRIPT_ENV] = previous_launcher


def launcher_script_path(
    current_file: str | os.PathLike[str], name: str | None = None
) -> Path:
    launcher = os.environ.get(LAUNCHER_SCRIPT_ENV)
    current = Path(current_file).resolve(strict=False)
    base = Path(launcher).resolve(strict=False) if launcher else current
    return base.with_name(name or current.name)


def _activate_runtime_lease(script_path: Path, environ: Mapping[str, str]) -> None:
    runtime_root = _runtime_root_for_script(
        script_path, codex_home=_codex_home(environ)
    )
    if runtime_root is None:
        return
    key = os.path.normcase(str(runtime_root.resolve(strict=False)))
    if key in _RUNTIME_LEASES:
        return
    lease_path = runtime_root / f".active.{os.getpid()}.json"
    stop = threading.Event()

    def touch() -> None:
        payload = {
            "pid": os.getpid(),
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        try:
            lease_path.write_text(
                json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
            )
        except OSError:
            pass

    def heartbeat() -> None:
        touch()
        while not stop.wait(RUNTIME_LEASE_REFRESH_SECONDS):
            touch()

    thread = threading.Thread(
        target=heartbeat, name=f"{PLUGIN_NAME}-runtime-lease", daemon=True
    )
    _RUNTIME_LEASES[key] = (stop, lease_path)
    thread.start()

    def cleanup() -> None:
        stop.set()
        try:
            lease_path.unlink()
        except OSError:
            pass

    atexit.register(cleanup)


def _runtime_root_for_script(script_path: Path, *, codex_home: Path) -> Path | None:
    parent = codex_home / "plugin-runtimes" / PLUGIN_NAME
    resolved_script = script_path.resolve(strict=False)
    for candidate in (resolved_script.parent, *resolved_script.parents):
        if os.path.normcase(
            str(candidate.parent.resolve(strict=False))
        ) != os.path.normcase(str(parent.resolve(strict=False))):
            continue
        if (candidate / METADATA_FILENAME).is_file():
            return candidate
    return None


def prepare_runtime_bootstrap(
    script_file: str | os.PathLike[str],
    *,
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    executable: str | None = None,
) -> RuntimeBootstrapPlan | None:
    env = os.environ if environ is None else environ
    if env.get(DISABLE_ENV):
        return None
    codex_home = _codex_home(env)
    script_path = Path(script_file).resolve(strict=False)
    source_root = find_plugin_root(script_path)
    if source_root is None or not _is_installed_cache_root(source_root, codex_home):
        return None
    relative_script = script_path.relative_to(source_root)
    runtime_root = ensure_runtime_copy(source_root, codex_home=codex_home)
    runtime_script = runtime_root / relative_script
    current_argv = tuple(sys.argv if argv is None else argv)
    next_argv = (
        str(executable or sys.executable),
        str(runtime_script),
        *current_argv[1:],
    )
    return RuntimeBootstrapPlan(
        source_root=source_root,
        source_script=source_root / relative_script,
        runtime_root=runtime_root,
        runtime_script=runtime_script,
        executable=str(executable or sys.executable),
        argv=next_argv,
    )


def _run_runtime_process(executable: str, argv: Sequence[str]) -> int:
    return subprocess.run(list(argv), executable=executable, check=False).returncode


def ensure_runtime_copy(source_root: Path, *, codex_home: Path | None = None) -> Path:
    resolved_source = source_root.resolve(strict=False)
    parent = (codex_home or _codex_home(os.environ)) / "plugin-runtimes" / PLUGIN_NAME
    parent.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest(resolved_source)
    version = str(manifest.get("version") or "0.0.0").strip() or "0.0.0"
    source_hash = content_hash_for_runtime(resolved_source)
    source_runtime_root = parent / f"{_safe_key_part(version)}-{source_hash[:16]}"
    if source_runtime_root.exists():
        _cleanup_stale_runtime_roots(parent, keep_root=source_runtime_root)
        return source_runtime_root

    temp_root = parent / f".staging.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    try:
        _copy_runtime_items(resolved_source, temp_root)
        manifest = _load_manifest(temp_root)
        version = str(manifest.get("version") or "0.0.0").strip() or "0.0.0"
        content_hash = content_hash_for_runtime(temp_root)
        runtime_key = f"{_safe_key_part(version)}-{content_hash[:16]}"
        runtime_root = parent / runtime_key
        if runtime_root.exists():
            _remove_temp_root(temp_root)
            _cleanup_stale_runtime_roots(parent, keep_root=runtime_root)
            return runtime_root
        _write_metadata(
            temp_root / METADATA_FILENAME,
            source_root=resolved_source,
            version=version,
            content_hash=content_hash,
            runtime_key=runtime_key,
        )
        _promote_temp_runtime(temp_root, runtime_root)
        _cleanup_stale_runtime_roots(parent, keep_root=runtime_root)
    except Exception:
        _remove_temp_root(temp_root)
        raise
    return runtime_root


def content_hash_for_runtime(source_root: Path) -> str:
    digest = hashlib.sha256()
    for path in _iter_runtime_files(source_root):
        relative = path.relative_to(source_root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def find_plugin_root(path: Path) -> Path | None:
    for candidate in (path.parent, *path.parents):
        manifest_path = candidate / ".codex-plugin" / "plugin.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            continue
        if str(manifest.get("name") or "") == PLUGIN_NAME:
            return candidate.resolve(strict=False)
    return None


def _codex_home(environ: Mapping[str, str]) -> Path:
    value = str(environ.get("CODEX_HOME") or "").strip()
    return (
        Path(value).expanduser().resolve(strict=False)
        if value
        else (Path.home() / ".codex").resolve(strict=False)
    )


def _is_installed_cache_root(source_root: Path, codex_home: Path) -> bool:
    cache_root = codex_home / "plugins" / "cache"
    return _is_relative_to(source_root, cache_root)


def _is_relative_to(child: Path, parent: Path) -> bool:
    child_text = os.path.normcase(str(child.resolve(strict=False)))
    parent_text = os.path.normcase(str(parent.resolve(strict=False)))
    try:
        return os.path.commonpath([child_text, parent_text]) == parent_text
    except ValueError:
        return False


def _load_manifest(source_root: Path) -> dict[str, object]:
    manifest_path = source_root / ".codex-plugin" / "plugin.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"invalid {PLUGIN_NAME} plugin manifest: {manifest_path}"
        ) from exc
    if str(manifest.get("name") or "") != PLUGIN_NAME:
        raise RuntimeError(f"plugin manifest is not {PLUGIN_NAME}: {manifest_path}")
    return dict(manifest)


def _iter_runtime_files(source_root: Path) -> list[Path]:
    files: list[Path] = []
    for item_name in RUNTIME_ITEMS:
        item = source_root / item_name
        if item.is_file() and not _excluded(item):
            files.append(item)
            continue
        if not item.is_dir():
            continue
        for path in item.rglob("*"):
            if path.is_file() and not _excluded(path):
                files.append(path)
    return sorted(files, key=lambda item: item.relative_to(source_root).as_posix())


def _excluded(path: Path) -> bool:
    if path.suffix in EXCLUDED_SUFFIXES:
        return True
    return any(part in EXCLUDED_NAMES for part in path.parts)


def _ignore_runtime_items(_directory: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name in EXCLUDED_NAMES or Path(name).suffix in EXCLUDED_SUFFIXES
    }


def _copy_runtime_items(source_root: Path, temp_root: Path) -> None:
    temp_root.mkdir(parents=True, exist_ok=False)
    for item_name in RUNTIME_ITEMS:
        source = source_root / item_name
        destination = temp_root / item_name
        if source.is_dir():
            shutil.copytree(source, destination, ignore=_ignore_runtime_items)
        elif source.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


def _write_metadata(
    path: Path,
    *,
    source_root: Path,
    version: str,
    content_hash: str,
    runtime_key: str,
) -> None:
    payload = {
        "plugin": PLUGIN_NAME,
        "source_path": str(source_root),
        "version": version,
        "content_hash": content_hash,
        "runtime_key": runtime_key,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _cleanup_stale_runtime_roots(parent: Path, *, keep_root: Path) -> None:
    try:
        roots = [
            path
            for path in parent.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        ]
    except OSError:
        return

    keep_text = os.path.normcase(str(keep_root.resolve(strict=False)))
    for root in roots:
        root_text = os.path.normcase(str(root.resolve(strict=False)))
        if root_text == keep_text or not (root / METADATA_FILENAME).is_file():
            continue
        if _runtime_has_active_lease(root):
            continue
        if _runtime_age_seconds(root) < RUNTIME_RETIRE_AFTER_SECONDS:
            continue
        try:
            shutil.rmtree(root)
        except OSError:
            pass


def _runtime_age_seconds(runtime_root: Path) -> float:
    metadata_path = runtime_root / METADATA_FILENAME
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        metadata = {}

    created_at = metadata.get("created_at")
    if isinstance(created_at, str):
        try:
            created = datetime.fromisoformat(
                created_at.replace("Z", "+00:00")
            ).timestamp()
            return time.time() - created
        except ValueError:
            pass

    try:
        return time.time() - runtime_root.stat().st_mtime
    except OSError:
        return 0.0


def _runtime_has_active_lease(runtime_root: Path) -> bool:
    for lease_path in runtime_root.glob(".active.*.json"):
        try:
            age = time.time() - lease_path.stat().st_mtime
        except OSError:
            continue
        if age < RUNTIME_LEASE_STALE_AFTER_SECONDS:
            return True
        try:
            lease_path.unlink()
        except OSError:
            pass
    return False


def _remove_temp_root(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _promote_temp_runtime(temp_root: Path, runtime_root: Path) -> None:
    for attempt in range(5):
        try:
            temp_root.rename(runtime_root)
            return
        except FileExistsError:
            _remove_temp_root(temp_root)
            return
        except PermissionError:
            if runtime_root.exists():
                _remove_temp_root(temp_root)
                return
            if attempt == 4:
                raise
            time.sleep(0.05)


def _safe_key_part(value: str) -> str:
    safe = "".join(
        char if char.isalnum() or char in {".", "-", "_"} else "-" for char in value
    )
    return safe.strip(".-_") or "0.0.0"
