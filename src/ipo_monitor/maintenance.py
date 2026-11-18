"""Offline operations CLI: status, audited event recovery and private DB backups."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

from . import CollectionError
from .daily import load_config, now


def status(config):
    """Read existing files only; never create a DB or read a credential."""
    result = {"workflow": config["workflow"], "enabled": config["enabled"], "last_run": None,
              "sources": [], "events": []}
    if config["ledger"].is_file():
        with closing(sqlite3.connect(config["ledger"].as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM daily_runs ORDER BY id DESC LIMIT 1").fetchone()
            if row:
                result["last_run"] = {
                    "day": row["day"], "status": row["status"],
                    "started_at": row["started_at"], "finished_at": row["finished_at"],
                    "summary": json.loads(row["summary_json"] or "{}"),
                }
    if config["db"].is_file():
        with closing(sqlite3.connect(config["db"].as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            result["sources"] = [
                {key: row[key] for key in ("source_id", "status", "error", "finished_at", "warning")
                 if key in row.keys()} for row in conn.execute(
                """SELECT * FROM source_attempts a
                WHERE id=(SELECT max(id) FROM source_attempts b WHERE b.source_id=a.source_id)
                ORDER BY source_id""")]
    if config.get("event_db") and config["event_db"].is_file():
        from .event_operations import list_events
        result["events"] = list_events(config, now().date())
    last = result["last_run"]
    result["health"] = ("never_run" if last is None else
                        "stale" if last["day"] != now().date().isoformat() else last["status"])
    result["healthy"] = result["health"] == "success"
    return result


def command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="로컬 상태·이벤트 복구·SQLite 백업 (API 호출 없음)")
    parser.add_argument("operation", choices=["status", "events", "backup", "restore"])
    parser.add_argument("--config", type=Path, default=Path("config/daily.toml"))
    parser.add_argument("--event", type=int)
    parser.add_argument("--action", choices=["close", "reopen", "retry-analysis", "retry-delivery", "mark-sent"])
    parser.add_argument("--reason", help="개별 복구의 운영자 판단 기록 (비밀정보 금지)")
    parser.add_argument("--confirm-duplicate-risk", action="store_true")
    parser.add_argument("--source", type=Path, help="복원할 백업 디렉터리")
    parser.add_argument("--output", type=Path, help="새 data/ 하위 디렉터리")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        match args.operation:
            case "status":
                result = status(config)
            case "events":
                from .event_operations import list_events, resolve_event
                if args.action:
                    if args.event is None or not args.reason:
                        raise CollectionError("event_and_reason_required")
                    result = resolve_event(config, args.event, args.action, args.reason,
                                           args.confirm_duplicate_risk)
                else:
                    if args.event is not None:
                        raise CollectionError("event_action_required")
                    result = {"events": list_events(config, now().date())}
            case "backup":
                from .backup import backup
                if args.output is None:
                    raise CollectionError("backup_output_required")
                result = backup(config, args.output)
            case "restore":
                from .backup import restore
                if args.output is None or args.source is None:
                    raise CollectionError("restore_source_and_output_required")
                result = restore(config, args.source, args.output)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (CollectionError, OSError, sqlite3.Error, ValueError, KeyError) as exc:
        code = str(exc) if isinstance(exc, CollectionError) else "maintenance_storage_invalid"
        print(json.dumps({"status": "error", "error_code": code}), file=sys.stderr)
        return 1
