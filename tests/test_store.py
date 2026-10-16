"""Check indexed queries and transactional upgrade of legacy ledgers."""

from __future__ import annotations

import sqlite3

import pytest

from server.store import Store


def test_image_budget_migrates_once_and_reservations_are_transactional(tmp_path, monkeypatch):
    path = tmp_path / "ledger.sqlite3"
    store = Store(path)
    with store.transaction() as db:
        db.execute("DROP TRIGGER image_bytes_reserved")
        db.execute("DELETE FROM metadata WHERE key='image_bytes'")
        db.execute("INSERT INTO image_ledger VALUES ('old','artifacts/old.png',123,'hash')")
    store.close()
    store = Store(path)
    assert store.image_bytes() == 123
    with pytest.raises(RuntimeError):
        with store.transaction() as db:
            db.execute("INSERT INTO image_ledger VALUES ('new','artifacts/new.png',456,'hash')")
            assert store.image_bytes() == 579
            raise RuntimeError("Rollback a failed result transaction")
    assert store.image_bytes() == 123
    with store.transaction() as db:
        db.execute("INSERT INTO image_ledger VALUES ('new','artifacts/new.png',456,'hash')")
        db.execute("INSERT OR IGNORE INTO image_ledger VALUES ('new','artifacts/new.png',456,'hash')")
        db.execute("DELETE FROM image_ledger WHERE request_id='old'")
    assert store.image_bytes() == 579
    store.close()
    traced = []
    connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(traced.append)
        return db

    monkeypatch.setattr("server.store.sqlite3.connect", traced_connect)
    store = Store(path)
    try:
        assert store.image_bytes() == 579
        assert not any("SUM(" in sql.upper() for sql in traced)
    finally:
        store.close()


def test_hot_queries_use_indexes_and_ignore_completed_rows(tmp_path):
    store = Store(tmp_path / "ledger.sqlite3")
    try:
        for query, index in (
            ("SELECT * FROM outbox WHERE published=0 ORDER BY id", "outbox_pending"),
            ("SELECT session_id FROM sessions WHERE state='idle' AND closed=0 AND last_activity<=0", "sessions_idle"),
            ("SELECT * FROM requests WHERE state='queued' ORDER BY rowid", "requests_state"),
            ("SELECT * FROM requests WHERE state='running' ORDER BY rowid", "requests_state"),
            ("SELECT sessions.workspace FROM requests JOIN sessions USING(session_id) WHERE requests.state IN ('running','unknown')", "requests_state"),
        ):
            explanation = " ".join(str(tuple(row)) for row in store.connection.execute("EXPLAIN QUERY PLAN " + query))
            assert index in explanation
        with store.transaction() as db:
            db.execute("INSERT INTO sessions(session_id,settings_json,settings_hash,workspace,claude_session_id,last_activity,created_at) VALUES ('s','{}','h','/home/test','native',0,'now')")
            for state in ("queued", "running", "unknown", "completed"):
                db.execute("INSERT INTO requests(request_id,session_id,body_json,body_hash,state,accepted,received_at) VALUES (?,'s','{}','h',?,1,'now')", (state, state))
        assert [row["request_id"] for row in store.queued_requests()] == ["queued"]
        assert [row["request_id"] for row in store.running_requests()] == ["running"]
        assert store.blocked_workspaces() == ["/home/test", "/home/test"]
        assert store.has_live_requests()
    finally:
        store.close()
