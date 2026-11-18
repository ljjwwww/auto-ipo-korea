"""Private daily execution ledger; reservations are never refunded after a crash."""

from contextlib import contextmanager
from datetime import date, datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat

from ipo_monitor import CollectionError


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS daily_runs (
        id INTEGER PRIMARY KEY, day TEXT NOT NULL, started_at TEXT NOT NULL,
        finished_at TEXT, status TEXT NOT NULL, summary_json TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS daily_reservations (
        id INTEGER PRIMARY KEY, day TEXT NOT NULL, model TEXT NOT NULL,
        run_id INTEGER NOT NULL REFERENCES daily_runs(id), input_hash TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('reserved','dispatched','completed','failed')),
        input_tokens INTEGER, output_tokens INTEGER, http_status INTEGER)""")
    conn.execute("CREATE INDEX IF NOT EXISTS daily_reservations_day ON daily_reservations(day)")


def _day(value):
    try:
        valid = isinstance(value, str) and date.fromisoformat(value).isoformat() == value
    except ValueError:
        valid = False
    if not valid:
        raise CollectionError("일일 원장 날짜는 YYYY-MM-DD 형식이어야 합니다.")
    return value


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 512 or any(ord(c) < 32 for c in value):
        raise CollectionError("일일 원장 식별자가 올바르지 않습니다.")
    return value


def _integer(value, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise CollectionError("일일 원장 수치는 유효한 정수여야 합니다.")
    return value


def _now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def file_lock(path: Path):
    """Hold an OS lock without unlinking its inode, including on failed acquisition."""
    path = Path(path)
    fd = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise CollectionError("일일 실행 잠금 파일이 올바르지 않습니다.")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if fd is not None:
            os.close(fd)
        raise CollectionError("일일 실행 잠금을 확보할 수 없습니다. 다른 실행과 경로 권한을 확인하세요.") from None
    except CollectionError:
        if fd is not None:
            os.close(fd)
        raise
    try:
        yield
    finally:
        os.close(fd)


class DailyLedger:
    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        ensure_schema(self.conn)
        self.conn.commit()
        path.chmod(0o600)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.conn.close()

    def start_run(self, day):
        with self.conn:
            return self.conn.execute("INSERT INTO daily_runs(day,started_at,status) VALUES (?,?,'running')",
                                     (_day(day), _now())).lastrowid

    def finish_run(self, run_id, status, summary):
        if status not in {'success', 'partial', 'error', 'disabled', 'skipped', 'dry_run'} or not isinstance(summary, dict):
            raise CollectionError("일일 실행 결과가 올바르지 않습니다.")
        try:
            encoded = json.dumps(summary, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            raise CollectionError("일일 실행 요약은 JSON 데이터여야 합니다.") from None
        with self.conn:
            result = self.conn.execute("UPDATE daily_runs SET status=?,summary_json=?,finished_at=? WHERE id=? AND status='running'",
                                       (status, encoded, _now(), _integer(run_id, 1)))
            if result.rowcount != 1:
                raise CollectionError("종료할 실행을 찾을 수 없습니다.")

    def reserve(self, day, model, run_id, input_hash, limit):
        day, model, input_hash = _day(day), _text(model), _text(input_hash)
        _integer(limit); _integer(run_id, 1)
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            if not self.conn.execute("SELECT 1 FROM daily_runs WHERE id=? AND day=? AND status='running'", (run_id, day)).fetchone():
                raise CollectionError("예약 대상 실행이 올바르지 않습니다.")
            if self.conn.execute("SELECT count(*) FROM daily_reservations WHERE day=?", (day,)).fetchone()[0] >= limit:
                return None
            return self.conn.execute("INSERT INTO daily_reservations(day,model,run_id,input_hash,state) VALUES (?,?,?,?,'reserved')",
                                     (day, model, run_id, input_hash)).lastrowid

    def mark_dispatched(self, reservation_id):
        with self.conn:
            result = self.conn.execute("UPDATE daily_reservations SET state='dispatched' WHERE id=? AND state='reserved'",
                                       (_integer(reservation_id, 1),))
            if result.rowcount != 1:
                raise CollectionError("전송할 예약을 찾을 수 없습니다.")

    def complete(self, reservation_id, usage=None, status='completed'):
        if status not in {'completed', 'failed'} or (usage is not None and not isinstance(usage, dict)):
            raise CollectionError("요청 결과가 올바르지 않습니다.")
        usage = usage or {}
        values = [usage.get(key) for key in ('input_tokens', 'output_tokens', 'http_status')]
        for value in values:
            if value is not None:
                _integer(value)
        if values[2] is not None and not 100 <= values[2] <= 599:
            raise CollectionError("HTTP 상태 코드가 올바르지 않습니다.")
        with self.conn:
            result = self.conn.execute("""UPDATE daily_reservations SET state=?,input_tokens=?,output_tokens=?,http_status=?
                WHERE id=? AND state IN ('reserved','dispatched')""", (status, *values, _integer(reservation_id, 1)))
            if result.rowcount != 1:
                raise CollectionError("완료할 예약을 찾을 수 없습니다.")

    def remaining(self, day, limit):
        _integer(limit)
        count = self.conn.execute("SELECT count(*) FROM daily_reservations WHERE day=?", (_day(day),)).fetchone()[0]
        return max(0, limit - count)

    def attempts_for(self, input_hash, model):
        return self.conn.execute("SELECT count(*) FROM daily_reservations WHERE input_hash=? AND model=?",
                                 (_text(input_hash), _text(model))).fetchone()[0]

    def summary(self, day=None):
        clause, args = (" WHERE day=?", (_day(day),)) if day is not None else ("", ())
        states = dict.fromkeys(('reserved', 'dispatched', 'completed', 'failed'), 0)
        for row in self.conn.execute("SELECT state,count(*) FROM daily_reservations" + clause + " GROUP BY state", args):
            states[row[0]] = row[1]
        totals = self.conn.execute("SELECT coalesce(sum(input_tokens),0),coalesce(sum(output_tokens),0) FROM daily_reservations" + clause, args).fetchone()
        run = self.conn.execute("SELECT * FROM daily_runs" + clause + " ORDER BY id DESC LIMIT 1", args).fetchone()
        latest = dict(run) if run else None
        if latest is not None:
            latest['summary'] = json.loads(latest.pop('summary_json') or '{}')
        return dict(day=day, reservations=sum(states.values()), states=states,
                    input_tokens=totals[0], output_tokens=totals[1], last_run=latest)
