"""Real offline CLI coverage for operator status, recovery and SQLite snapshots."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from ipo_monitor.daily_state import DailyLedger
from ipo_monitor.mention_delivery import _schema
from ipo_monitor.mentions import ensure_schema
from ipo_monitor.multisource import connect


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.config = self.root / "daily.toml"
        self.config.write_text('''[daily]
project_root="."
workflow="mention_events"
event_db="data/events.sqlite3"
enabled=true
backend="ollama"
model="test-model"
offline=true
sources="sources.toml"
db="data/sources.sqlite3"
ledger="data/ledger.sqlite3"
aliases="stocks.toml"
policy="policy.toml"
log="data/daily.jsonl"
max_calls_per_day=600
max_attempts=3
retry_hours=20
''')

    def call(self, operation, *args):
        return subprocess.run(
            [sys.executable, "-m", "ipo_monitor", operation, "--config", str(self.config), *args],
            capture_output=True, text=True, timeout=15)

    def populate(self):
        with closing(connect(self.data / "sources.sqlite3")):
            pass
        with DailyLedger(self.data / "ledger.sqlite3") as ledger:
            run = ledger.start_run("2026-11-18")
            ledger.finish_run(run, "success", {"requests": 0, "phase": "complete"})
        with closing(sqlite3.connect(self.data / "events.sqlite3")) as conn, conn:
            ensure_schema(conn)
            conn.execute("""INSERT INTO mention_events
                (id,event_hash,stock,source_id,source_type,item_id,snapshot_id,input_hash,
                input_json,created_at,analysis_status,error_code)
                VALUES (1,'hash','가상기업','blog','rss','item',1,'input','{}','now','error','test_error')""")
            conn.execute("INSERT INTO mention_catalog VALUES (?,?,?)",
                         ("2099-01", "가상기업", '{"end":"2099-01-31"}'))
            _schema(conn)
            conn.execute("""INSERT INTO mention_delivery
                VALUES (1,'private-destination','unknown',3,NULL,'timeout','2026-11-18','now')""")

    def test_status_without_state_creates_no_database_and_reads_no_secret(self):
        result = self.call("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(json.loads(result.stdout)["last_run"])
        self.assertEqual(list(self.data.iterdir()), [])

    def test_status_reads_existing_state_without_changing_database_bytes(self):
        self.populate()
        before = {path.name: path.read_bytes() for path in self.data.glob("*.sqlite3")}
        result = self.call("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["last_run"]["status"], "success")
        self.assertEqual(report["events"][0]["delivery"][0]["state"], "unknown")
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.data.glob("*.sqlite3")})
        self.assertNotIn("private-destination", result.stdout)

    def test_unknown_retry_requires_confirmation_then_preserves_audit_and_quota(self):
        self.populate()
        arguments = ("--event", "1", "--action", "retry-delivery", "--reason", "운영자가 재전송 결정")
        rejected = self.call("events", *arguments)
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("duplicate_risk", rejected.stderr)
        with closing(sqlite3.connect(self.data / "events.sqlite3")) as conn:
            self.assertEqual(conn.execute("SELECT state,attempts FROM mention_delivery").fetchone(), ("unknown", 3))
        accepted = self.call("events", *arguments, "--confirm-duplicate-risk")
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        with closing(sqlite3.connect(self.data / "events.sqlite3")) as conn:
            self.assertEqual(conn.execute("SELECT state,attempts FROM mention_delivery").fetchone(), ("failed", 0))
            self.assertEqual(conn.execute("SELECT action FROM mention_event_actions").fetchall(), [("retry-delivery",)])
        with closing(sqlite3.connect(self.data / "ledger.sqlite3")) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM daily_runs").fetchone()[0], 1)

    def test_close_and_analysis_retry_are_individual_audited_actions(self):
        self.populate()
        self.assertEqual(self.call("events", "--event", "1", "--action", "close", "--reason", "종료").returncode, 0)
        report = json.loads(self.call("events").stdout)
        self.assertEqual(report["events"][0]["state"], "closed")
        retried = self.call("events", "--event", "1", "--action", "retry-analysis", "--reason", "다시 확인")
        self.assertEqual(retried.returncode, 0, retried.stderr)
        with closing(sqlite3.connect(self.data / "events.sqlite3")) as conn:
            self.assertEqual(conn.execute("SELECT analysis_status,attempts FROM mention_events").fetchone(), ("pending", 0))
            self.assertEqual(conn.execute("SELECT closed FROM mention_event_controls").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM mention_event_actions").fetchone()[0], 2)

    def test_backup_restore_preserves_rows_excludes_secrets_and_refuses_overwrite(self):
        self.populate()
        (self.data / "secrets").mkdir()
        (self.data / "secrets" / "test.key").write_text("test-only-not-a-real-key")
        bundle = self.data / "backups" / "one"
        saved = self.call("backup", "--output", str(bundle))
        self.assertEqual(saved.returncode, 0, saved.stderr)
        self.assertEqual({p.name for p in bundle.iterdir()},
                         {"manifest.json", "sources.sqlite3", "ledger.sqlite3", "events.sqlite3"})
        self.assertEqual((bundle / "events.sqlite3").stat().st_mode & 0o777, 0o600)
        restored = self.data / "restored"
        result = self.call("restore", "--source", str(bundle), "--output", str(restored))
        self.assertEqual(result.returncode, 0, result.stderr)
        with closing(sqlite3.connect(restored / "events.sqlite3")) as conn:
            self.assertEqual(conn.execute("SELECT event_hash FROM mention_events").fetchall(), [("hash",)])
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(self.call("backup", "--output", str(bundle)).returncode, 1)
        self.assertEqual(self.call("restore", "--source", str(bundle), "--output", str(restored)).returncode, 1)

    def test_restore_rejects_manifest_path_escape_without_creating_destination(self):
        self.populate()
        bundle = self.data / "backup"
        bundle.mkdir()
        (bundle / "manifest.json").write_text('{"version":1,"databases":{"events":"../events.sqlite3"}}')
        output = self.data / "restored"
        result = self.call("restore", "--source", str(bundle), "--output", str(output))
        self.assertEqual(result.returncode, 1)
        self.assertFalse(output.exists())

    def test_backup_includes_committed_wal_rows(self):
        self.populate()
        with closing(sqlite3.connect(self.data / "sources.sqlite3")) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE wal_probe (value TEXT)")
            conn.execute("INSERT INTO wal_probe VALUES ('committed')")
            conn.commit()
            bundle = self.data / "wal-backup"
            saved = self.call("backup", "--output", str(bundle))
            self.assertEqual(saved.returncode, 0, saved.stderr)
            with closing(sqlite3.connect(bundle / "sources.sqlite3")) as copy:
                self.assertEqual(copy.execute("SELECT * FROM wal_probe").fetchall(), [("committed",)])

    def test_expired_event_is_separate_from_pending_even_after_catalog_removal(self):
        self.populate()
        with closing(sqlite3.connect(self.data / "events.sqlite3")) as conn, conn:
            conn.execute("UPDATE mention_events SET catalog_end='2000-01-01'")
        result = self.call("events")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["events"][0]["state"], "expired")
