from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS state(name TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, at INTEGER, level TEXT, message TEXT);
                CREATE TABLE IF NOT EXISTS samples(mode TEXT, scope TEXT, at INTEGER, equity REAL,
                    PRIMARY KEY(mode, at));
                CREATE TABLE IF NOT EXISTS intervals(scope TEXT, first INTEGER, last INTEGER,
                    PRIMARY KEY(scope, first));
                CREATE TABLE IF NOT EXISTS trades(id TEXT PRIMARY KEY, mode TEXT, scope TEXT, at INTEGER,
                    pnl REAL, payload TEXT);
                CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, at INTEGER, status TEXT, progress TEXT,
                    spec TEXT, report TEXT);
                UPDATE runs SET status='failed', progress='Interrupted before completion'
                    WHERE status IN ('queued','running');
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, name, default=None):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM state WHERE name=?", (name,)).fetchone()
        return json.loads(row["payload"]) if row else default

    def state(self, name, value, trade=None):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (name, json.dumps(value, allow_nan=False)))
            if trade:
                db.execute("INSERT OR IGNORE INTO trades VALUES (?,?,?,?,?,?)", (
                    trade["id"], trade["mode"], trade["scope"], trade["closed"], trade["pnl"], json.dumps(trade),
                ))

    def log(self, level, message):
        with self.connect() as db:
            db.execute("INSERT INTO events(at, level, message) VALUES (?,?,?)", (int(time.time()), level, message[:1500]))

    def sample(self, mode, scope, stamp, equity, healthy, *, continuous=True):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO samples VALUES (?,?,?,?)", (mode, scope, stamp, equity))
            if healthy and mode == "paper":
                row = db.execute("SELECT first,last FROM intervals WHERE scope=? ORDER BY first DESC LIMIT 1",
                                 (scope,)).fetchone()
                if continuous and row and 0 <= stamp - row["last"] <= 60:
                    db.execute("UPDATE intervals SET last=? WHERE scope=? AND first=?", (stamp, scope, row["first"]))
                else:
                    db.execute("INSERT OR IGNORE INTO intervals VALUES (?,?,?)", (scope, stamp, stamp))

    def evidence(self, scope):
        with self.connect() as db:
            intervals = db.execute("SELECT first,last FROM intervals WHERE scope=? ORDER BY first", (scope,)).fetchall()
            summary = db.execute("SELECT count(*) n,avg(pnl) expectancy FROM trades WHERE mode='paper' AND scope=?",
                                 (scope,)).fetchone()
            samples = db.execute("SELECT equity FROM samples WHERE mode='paper' AND scope=? ORDER BY at", (scope,)).fetchall()
        return [tuple(row) for row in intervals], dict(summary), [row["equity"] for row in samples]

    def chart(self, mode, scope, since):
        with self.connect() as db:
            return [tuple(row) for row in db.execute("""
                SELECT s.at, s.equity FROM samples s JOIN (
                    SELECT max(at) at FROM samples WHERE mode=? AND scope=? AND at>=? GROUP BY at/300
                ) mark ON mark.at=s.at WHERE s.mode=? AND s.scope=? ORDER BY s.at
                """, (mode, scope, since, mode, scope))]

    def baseline(self, mode, scope, end):
        with self.connect() as db:
            row = db.execute("SELECT equity FROM samples WHERE mode=? AND scope=? AND at<=? ORDER BY at DESC LIMIT 1",
                             (mode, scope, end)).fetchone()
            return row["equity"] if row else None

    def logs(self):
        with self.connect() as db:
            events = db.execute("SELECT at,level,message FROM events ORDER BY id DESC LIMIT 200").fetchall()
            trades = db.execute("SELECT payload FROM trades ORDER BY at DESC LIMIT 200").fetchall()
            return {"events": [dict(row) for row in events], "trades": [json.loads(row[0]) for row in trades]}

    def new_run(self, run_id, spec):
        with self.connect() as db:
            db.execute("INSERT INTO runs VALUES (?,?,?,?,?,NULL)",
                       (run_id, int(time.time()), "queued", "Queued", json.dumps(spec)))

    def update_run(self, run_id, status, progress, report=None):
        with self.connect() as db:
            db.execute("UPDATE runs SET status=?,progress=?,report=? WHERE id=?",
                       (status, progress, json.dumps(report, allow_nan=False) if report else None, run_id))

    def complete_tournament(self, run_id, report):
        with self.connect() as db:
            encoded = json.dumps(report, allow_nan=False)
            db.execute("""
                INSERT INTO runs VALUES (?, ?, 'complete', 'Complete', ?, ?)
                ON CONFLICT(id) DO UPDATE SET status='complete',progress='Complete',report=excluded.report
                """, (run_id, int(time.time()), json.dumps(report["spec"]), encoded))
            if report["qualification"]:
                release = {
                    "run_id": run_id,
                    "plans": {name: preset["deployment"] for name, preset in report["presets"].items()},
                    "status": {name: "EDGE CONFIRMED" if preset["deployment"] else "NO EDGE FOUND"
                               for name, preset in report["presets"].items()},
                }
                # Freeze deployed evidence across reruns so an open position keeps its exact rule set.
                db.execute("INSERT OR IGNORE INTO state VALUES ('edge_release', ?)", (json.dumps(release),))

    def runs(self, run_id=None):
        with self.connect() as db:
            if run_id:
                row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
                if row:
                    record = dict(row)
                    for key in ("spec", "report"):
                        record[key] = json.loads(record[key]) if record[key] else None
                    return record
                return None
            rows = db.execute("SELECT id,at,status,progress,spec FROM runs ORDER BY at DESC LIMIT 100").fetchall()
            return [{**dict(row), "spec": json.loads(row["spec"])} for row in rows]
