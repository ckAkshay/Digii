from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Generator

from fastapi import HTTPException

from .chain import canonical_event_body
from .config import settings


def init_db(path: Path | None = None) -> None:
    con = sqlite3.connect(path or settings.db_path)
    try:
        con.execute("PRAGMA foreign_keys = ON")
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('instructor','student')));
        CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, title TEXT NOT NULL, instructor_id TEXT NOT NULL REFERENCES users(id), secret TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), student_id TEXT NOT NULL REFERENCES users(id), type TEXT NOT NULL, payload TEXT NOT NULL, actor_id TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS events_session ON events(session_id, created_at);
        CREATE INDEX IF NOT EXISTS events_student ON events(session_id, student_id);
        CREATE UNIQUE INDEX IF NOT EXISTS one_checkin_per_student_session ON events(session_id, student_id) WHERE type='CHECKIN';
        CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
        """)
        con.executemany("INSERT OR IGNORE INTO users(id,name,role) VALUES(?,?,?)", [
            ("instructor-1", "Demo Instructor", "instructor"),
            ("student-1", "Demo Student", "student"),
            ("student-2", "Demo Student Two", "student"),
        ])
        con.commit()
    finally:
        con.close()


def get_db() -> Generator[sqlite3.Connection, None, None]:
    # check_same_thread=False: FastAPI resolves a sync generator dependency's
    # open, the route body, and its close as separate run_in_threadpool calls,
    # which are not guaranteed to land on the same worker thread.
    con = sqlite3.connect(settings.db_path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def get_session(con: sqlite3.Connection, sid: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if not row:
        raise HTTPException(404, "Session not found")
    return row


def append_event(con: sqlite3.Connection, session_id: str, student_id: str, kind: str, payload: dict[str, Any], actor_id: str, when: datetime) -> sqlite3.Row:
    # Serialize appends so concurrent requests cannot fork a session's chain.
    con.execute("BEGIN IMMEDIATE")
    last = con.execute("SELECT hash FROM events WHERE session_id=? ORDER BY rowid DESC LIMIT 1", (session_id,)).fetchone()
    prev = last["hash"] if last else "0" * 64
    eid, created = str(uuid.uuid4()), when.isoformat()
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    body = canonical_event_body(id=eid, session_id=session_id, student_id=student_id, type=kind,
                                 payload=json.loads(encoded), actor_id=actor_id, created_at=created,
                                 prev_hash=prev)
    digest = hashlib.sha256((prev + body).encode()).hexdigest()
    con.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)", (eid, session_id, student_id, kind, encoded, actor_id, created, prev, digest))
    return con.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
