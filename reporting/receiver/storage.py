"""Private, bounded SQLite storage for accepted diagnostic reports."""

import hashlib
import json
import os
import sqlite3
import stat
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


RETENTION_SECONDS = 30 * 86400


class Conflict(Exception):
    pass


class Full(Exception):
    pass


class Store:
    def __init__(self, path, *, max_reports=10_000, clock=time.time, prune_on_open=True):
        self.path = Path(path)
        self.clock = clock
        self.max_reports = max_reports
        self.lock = threading.Lock()
        if self.path.is_symlink() or (self.path.exists() and not stat.S_ISREG(self.path.stat().st_mode)):
            raise ValueError("database path must be a regular file")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.parent.is_symlink():
            raise ValueError("database directory must not be a symlink")
        if self.path.parent.stat().st_mode & 0o077:
            raise ValueError("database directory must be private (0700)")
        old_umask = os.umask(0o077)
        try:
            self.db = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        finally:
            os.umask(old_umask)
        os.chmod(self.path, 0o600)
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA secure_delete=ON")
        page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
        max_pages = 32 * 1024 * 1024 // page_size
        if self.db.execute(f"PRAGMA max_page_count={max_pages}").fetchone()[0] > max_pages:
            raise ValueError("database already exceeds 32 MiB")
        self.db.execute("""CREATE TABLE IF NOT EXISTS reports (
            report_id TEXT PRIMARY KEY, digest TEXT NOT NULL, receipt_id TEXT NOT NULL UNIQUE,
            accepted_at TEXT NOT NULL, accepted_epoch REAL NOT NULL, body BLOB NOT NULL
        )""")
        self.db.execute("CREATE INDEX IF NOT EXISTS reports_age ON reports(accepted_epoch)")
        self.db.commit()
        if prune_on_open:
            self.prune()

    def close(self):
        self.db.close()

    def prune(self, now=None):
        now = self.clock() if now is None else now
        with self.lock, self.db:
            cursor = self.db.execute("DELETE FROM reports WHERE accepted_epoch <= ?", (now - RETENTION_SECONDS,))
            return cursor.rowcount

    def count(self):
        with self.lock:
            return self.db.execute("SELECT count(*) FROM reports").fetchone()[0]

    def accept(self, report_id, body):
        digest = hashlib.sha256(body).hexdigest()
        with self.lock:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                row = self.db.execute(
                    "SELECT digest, receipt_id, accepted_at FROM reports WHERE report_id = ?", (report_id,)
                ).fetchone()
                if row:
                    if row[0] != digest:
                        raise Conflict()
                    receipt = {"report_id": report_id, "receipt_id": row[1], "accepted_at": row[2]}
                else:
                    if self.db.execute("SELECT count(*) FROM reports").fetchone()[0] >= self.max_reports:
                        raise Full()
                    accepted_epoch = self.clock()
                    accepted_at = datetime.fromtimestamp(accepted_epoch, timezone.utc).isoformat().replace("+00:00", "Z")
                    receipt = {"report_id": report_id, "receipt_id": uuid.uuid4().hex, "accepted_at": accepted_at}
                    self.db.execute(
                        "INSERT INTO reports VALUES (?, ?, ?, ?, ?, ?)",
                        (report_id, digest, receipt["receipt_id"], accepted_at, accepted_epoch, body),
                    )
        return receipt

    def counts(self):
        with self.lock:
            rows = self.db.execute("SELECT json_extract(body, '$.mode'), count(*) FROM reports GROUP BY 1").fetchall()
            return {"total": sum(row[1] for row in rows), "by_mode": dict(rows)}

    def inspect(self, receipt_id):
        with self.lock:
            row = self.db.execute(
                "SELECT accepted_at, body FROM reports WHERE receipt_id = ?", (receipt_id,)
            ).fetchone()
        if not row:
            return None
        return {"receipt_id": receipt_id, "accepted_at": row[0], "report": json.loads(row[1])}

    def delete(self, receipt_id):
        with self.lock, self.db:
            return self.db.execute("DELETE FROM reports WHERE receipt_id = ?", (receipt_id,)).rowcount
