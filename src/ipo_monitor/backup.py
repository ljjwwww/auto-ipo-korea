"""Private SQLite snapshots and restore into new directories, never over live files."""
from contextlib import ExitStack, closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
from typing import Final

from . import CollectionError
from .daily_state import file_lock

DATABASES: Final = {"sources", "ledger", "events", "telegram"}


def _directory(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    if path.is_symlink() or not resolved.is_relative_to(root / "data"):
        raise CollectionError("backup_path_outside_private_data")
    return resolved


def _copy_database(source: Path, target: Path) -> None:
    """Use SQLite's online backup API, including committed WAL data."""
    if source.is_symlink() or not source.is_file():
        raise CollectionError("backup_source_invalid")
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as origin:
        with closing(sqlite3.connect(target)) as destination:
            origin.backup(destination)
            if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise CollectionError("backup_integrity_failed")


def backup(config, output: Path) -> dict[str, str]:
    """Snapshot configured DBs only; credentials and source files are excluded."""
    output = _directory(output, config["root"])
    sources = {"sources": config["db"], "ledger": config["ledger"]}
    if config.get("event_db"):
        sources["events"] = config["event_db"]
    if config.get("telegram_config"):
        from .telegram_command import load_config
        sources["telegram"] = load_config(config["telegram_config"])["state_db"]
    sources = {name: path for name, path in sources.items() if path.is_file()}
    if not sources:
        raise CollectionError("backup_no_databases")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(mode=0o700)  # Exclusive: an existing snapshot is never overwritten.
    try:
        with ExitStack() as stack:
            stack.enter_context(file_lock(config["ledger"].with_suffix(".lock")))
            if config.get("event_db"):
                stack.enter_context(file_lock(config["event_db"].with_suffix(".delivery.lock")))
            files = {}
            for name, source in sources.items():
                filename = name + ".sqlite3"
                _copy_database(source, output / filename)
                files[name] = filename
            descriptor = os.open(output / "manifest.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "databases": files}, handle, sort_keys=True)
        return {"status": "backed_up", "directory": str(output)}
    except (OSError, sqlite3.Error, CollectionError):
        shutil.rmtree(output)
        raise


def restore(config, source: Path, output: Path) -> dict[str, str]:
    """Restore a validated bundle to a new private directory for inspection."""
    source = _directory(source, config["root"])
    output = _directory(output, config["root"])
    manifest = source / "manifest.json"
    if manifest.is_symlink() or manifest.stat().st_size > 16_384:
        raise CollectionError("backup_manifest_invalid")
    value = json.loads(manifest.read_text(encoding="utf-8"))
    files = value.get("databases") if isinstance(value, dict) else None
    if (not isinstance(files, dict) or not files or value.get("version") != 1
            or not set(files).issubset(DATABASES)
            or any(filename != name + ".sqlite3" for name, filename in files.items())):
        raise CollectionError("backup_manifest_invalid")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(mode=0o700)
    try:
        for filename in files.values():
            _copy_database(source / filename, output / filename)
        return {"status": "restored", "directory": str(output)}
    except (OSError, sqlite3.Error, CollectionError):
        shutil.rmtree(output)
        raise
