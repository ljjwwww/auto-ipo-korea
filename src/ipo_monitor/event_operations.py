"""Read-only event inventory and explicitly audited operator recovery."""
from contextlib import closing
from datetime import date, datetime, timezone
import json
import sqlite3
from typing import Literal

from . import CollectionError
from .daily_state import file_lock
from .event_reasons import ReasonCode

Action = Literal["close", "reopen", "retry-analysis", "retry-delivery", "mark-sent"]


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def list_events(config, today: date):
    path = config.get("event_db")
    if path is None or not path.is_file():
        return []
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        tables = _tables(conn)
        if "mention_events" not in tables:
            return []
        controls = {row["event_id"]: dict(row) for row in conn.execute(
            "SELECT * FROM mention_event_controls")} if "mention_event_controls" in tables else {}
        catalog = {row["stock"]: json.loads(row["payload_json"]) for row in conn.execute(
            "SELECT stock,payload_json FROM mention_catalog WHERE month=?", (today.strftime("%Y-%m"),))}
        records = []
        histories = {}
        if "mention_analysis_attempts" in tables:
            for attempt in conn.execute("""SELECT event_id,reason_code,reason_text,model,error_code,created_at
                    FROM mention_analysis_attempts ORDER BY id"""):
                code = attempt["reason_code"]
                histories.setdefault(attempt["event_id"], []).append({
                    "code": code, "label": ReasonCode(code).label if code is not None else None,
                    "text": attempt["reason_text"], "model": attempt["model"],
                    "validated": attempt["error_code"] is None,
                    "error_code": attempt["error_code"], "created_at": attempt["created_at"],
                })
        for row in conn.execute("SELECT * FROM mention_events ORDER BY id"):
            end = catalog.get(row["stock"], {}).get("end")
            remembered_end = row["catalog_end"] if "catalog_end" in row.keys() else None
            closed = bool(controls.get(row["id"], {}).get("closed"))
            receipts = [dict(receipt) for receipt in conn.execute(
                "SELECT state,attempts,error_code FROM mention_delivery WHERE event_id=?",
                (row["id"],))] if "mention_delivery" in tables else []
            expired = (end or remembered_end or "9999-12-31") < today.isoformat()
            state = ("closed" if closed else "expired" if expired else "not_in_catalog" if end is None
                     else "analysis_pending" if row["analysis_status"] != "success"
                     else "ready")
            if state == "ready" and receipts:
                states = {receipt["state"] for receipt in receipts}
                state = ("delivery_unknown" if states & {"unknown", "sending"} else
                         "delivery_failed" if "failed" in states else "sent")
            history = histories.get(row["id"], [])
            decision_reason = history[-1] if history else None
            if decision_reason is None and row["analysis_json"]:
                previous = json.loads(row["analysis_json"])
                code = previous.get("reason_code")
                decision_reason = {
                    "code": code, "label": ReasonCode(code).label if code is not None else None,
                    "text": previous.get("reason"), "model": None,
                    "validated": row["analysis_status"] == "success", "error_code": row["error_code"],
                }
            records.append({"event": row["id"], "stock": row["stock"], "source": row["source_type"],
                            "state": state, "analysis": row["analysis_status"],
                            "error_code": row["error_code"], "attempts": row["attempts"],
                            "delivery": receipts, "decision_reason": decision_reason,
                            "reason_history": history})
        return records


def resolve_event(config, event: int, action: Action, reason: str, confirm: bool = False):
    """Change one event while preserving the daily quota and append-only audit."""
    path = config.get("event_db")
    if path is None or not path.is_file() or path.is_symlink() or event < 1:
        raise CollectionError("event_not_found")
    if (not reason.strip() or len(reason) > 300
            or any(ord(char) < 32 or ord(char) == 127 for char in reason)):
        raise CollectionError("event_reason_invalid")
    stamp = datetime.now(timezone.utc).isoformat()
    with file_lock(config["ledger"].with_suffix(".lock")), file_lock(path.with_suffix(".delivery.lock")):
        with closing(sqlite3.connect(path, timeout=30)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM mention_events WHERE id=?", (event,)).fetchone()
            if row is None:
                raise CollectionError("event_not_found")
            tables = _tables(conn)
            receipts = list(conn.execute("SELECT * FROM mention_delivery WHERE event_id=?", (event,))) \
                if "mention_delivery" in tables else []
            if action == "retry-delivery":
                if not receipts or any(r["state"] == "sent" for r in receipts):
                    raise CollectionError("event_delivery_not_retryable")
                if any(r["state"] in {"sending", "unknown"} for r in receipts) and not confirm:
                    raise CollectionError("event_duplicate_risk_confirmation_required")
            if action == "mark-sent" and not any(r["state"] in {"sending", "unknown"} for r in receipts):
                raise CollectionError("event_delivery_not_unknown")
            conn.execute("""CREATE TABLE IF NOT EXISTS mention_event_controls (
                event_id INTEGER PRIMARY KEY,closed INTEGER NOT NULL DEFAULT 0,
                analysis_retry_at TEXT)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS mention_event_actions (
                id INTEGER PRIMARY KEY,event_id INTEGER NOT NULL,action TEXT NOT NULL,
                reason TEXT NOT NULL,created_at TEXT NOT NULL,previous_json TEXT NOT NULL)""")
            conn.execute("INSERT OR IGNORE INTO mention_event_controls(event_id) VALUES (?)", (event,))
            match action:
                case "close" | "reopen":
                    conn.execute("UPDATE mention_event_controls SET closed=? WHERE event_id=?",
                                 (int(action == "close"), event))
                case "retry-analysis":
                    conn.execute("UPDATE mention_event_controls SET closed=0,analysis_retry_at=? WHERE event_id=?",
                                 (stamp, event))
                    conn.execute("""UPDATE mention_events SET analysis_status='pending',
                        error_code=NULL,attempts=0 WHERE id=?""", (event,))
                    if "mention_analysis_cache" in tables:
                        conn.execute("DELETE FROM mention_analysis_cache WHERE input_hash=?", (row["input_hash"],))
                case "retry-delivery":
                    conn.execute("""UPDATE mention_delivery SET state='failed',attempts=0,
                        last_attempt_day='',error_code='operator_retry',updated_at=? WHERE event_id=?""",
                        (stamp, event))
                case "mark-sent":
                    conn.execute("""UPDATE mention_delivery SET state='sent',
                        error_code='operator_confirmed',updated_at=? WHERE event_id=?
                        AND state IN ('sending','unknown')""", (stamp, event))
            previous = {"analysis_status": row["analysis_status"], "error_code": row["error_code"],
                        "delivery": [{"state": r["state"], "attempts": r["attempts"]} for r in receipts]}
            conn.execute("""INSERT INTO mention_event_actions
                (event_id,action,reason,created_at,previous_json) VALUES (?,?,?,?,?)""",
                (event, action, reason, stamp, json.dumps(previous, ensure_ascii=False)))
    return {"status": "resolved", "event": event, "action": action, "network_requests": 0}
