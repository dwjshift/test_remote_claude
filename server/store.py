"""Durable execution journal and transactional publication outbox."""

from __future__ import annotations

import base64
import contextlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Iterator


class Store:
    """Keep execution and publication independent across process restarts."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY, settings_json TEXT NOT NULL,
                settings_hash TEXT NOT NULL, workspace TEXT NOT NULL,
                claude_session_id TEXT NOT NULL, initialized INTEGER NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'idle',
                active_request_id TEXT, closed INTEGER NOT NULL DEFAULT 0,
                last_activity REAL NOT NULL, created_at TEXT NOT NULL, error TEXT
            );
            CREATE TABLE IF NOT EXISTS requests (
                request_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id),
                body_json TEXT NOT NULL, body_hash TEXT NOT NULL,
                state TEXT NOT NULL, accepted INTEGER NOT NULL,
                received_at TEXT NOT NULL, started_at TEXT, ended_at TEXT,
                pid INTEGER, process_start TEXT, process_boot TEXT, result_json TEXT
            );
            CREATE TABLE IF NOT EXISTS controls (
                control_id TEXT PRIMARY KEY, body_hash TEXT NOT NULL, result_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT, dedupe_key TEXT NOT NULL UNIQUE,
                files_json TEXT NOT NULL, mutable_json TEXT NOT NULL,
                request_id TEXT, session_id TEXT, terminal_state TEXT,
                published INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS image_ledger (
                request_id TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL,
                sha256 TEXT NOT NULL, PRIMARY KEY(request_id, path)
            );
            CREATE INDEX IF NOT EXISTS requests_state ON requests(state);
            CREATE INDEX IF NOT EXISTS requests_session ON requests(session_id);
        """)

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except BaseException:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def get_meta(self, key: str, default=None):
        with self.lock:
            row = self.connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_meta(self, key: str, value) -> None:
        with self.lock:
            self.connection.execute(
                "INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    def get_session(self, session_id: str) -> dict | None:
        with self.lock:
            row = self.connection.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            return dict(row) if row else None

    def get_request(self, request_id: str) -> dict | None:
        with self.lock:
            row = self.connection.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
            return dict(row) if row else None

    def list_sessions(self) -> list[dict]:
        with self.lock:
            return [dict(row) for row in self.connection.execute("SELECT * FROM sessions ORDER BY created_at, session_id")]

    def list_requests(self, session_id: str | None = None) -> list[dict]:
        with self.lock:
            if session_id:
                rows = self.connection.execute("SELECT * FROM requests WHERE session_id=? ORDER BY rowid", (session_id,))
            else:
                rows = self.connection.execute("SELECT * FROM requests ORDER BY rowid")
            return [dict(row) for row in rows]

    def image_bytes(self) -> int:
        with self.lock:
            return self.connection.execute("SELECT COALESCE(SUM(size),0) FROM image_ledger").fetchone()[0]

    def pending(self) -> list[dict]:
        with self.lock:
            return [dict(row) for row in self.connection.execute("SELECT * FROM outbox WHERE published=0 ORDER BY id")]

    @staticmethod
    def enqueue(db, key: str, files: dict[str, bytes], mutable: set[str] | None = None,
                request_id: str | None = None, session_id: str | None = None,
                terminal_state: str | None = None) -> None:
        encoded = {name: base64.b64encode(data).decode("ascii") for name, data in files.items()}
        db.execute(
            "INSERT OR IGNORE INTO outbox(dedupe_key,files_json,mutable_json,request_id,session_id,terminal_state) VALUES (?,?,?,?,?,?)",
            (key, json.dumps(encoded), json.dumps(sorted(mutable or [])), request_id, session_id, terminal_state),
        )

    def acknowledge(self, rows: list[dict]) -> None:
        with self.transaction() as db:
            for row in rows:
                db.execute("UPDATE outbox SET published=1 WHERE id=?", (row["id"],))
                if row["terminal_state"]:
                    db.execute("UPDATE requests SET state=? WHERE request_id=?", (row["terminal_state"], row["request_id"]))
                    if row["terminal_state"] != "rejected":
                        db.execute(
                            "UPDATE sessions SET state=CASE WHEN closed=1 THEN 'closed' ELSE 'idle' END, active_request_id=NULL "
                            "WHERE session_id=? AND active_request_id=?",
                            (row["session_id"], row["request_id"]),
                        )

    def close(self) -> None:
        with self.lock:
            self.connection.close()
