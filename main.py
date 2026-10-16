from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

DB_PATH = Path(os.getenv("ATTENDANCE_DB", "attendance.db"))
RECEIPT_KEY = os.getenv("RECEIPT_KEY", "development-receipt-key-change-me").encode()
CODE_PERIOD = 30


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Attendance & Participation", version="0.1.0", lifespan=lifespan)


@app.get("/", include_in_schema=False)
def home():
    """Send browser visitors to the interactive API documentation."""
    return RedirectResponse(url="/docs")


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
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


def init_db() -> None:
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('instructor','student')));
        CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, title TEXT NOT NULL, instructor_id TEXT NOT NULL REFERENCES users(id), secret TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), student_id TEXT NOT NULL REFERENCES users(id), type TEXT NOT NULL, payload TEXT NOT NULL, actor_id TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS events_session ON events(session_id, created_at);
        CREATE INDEX IF NOT EXISTS events_student ON events(session_id, student_id);
        CREATE UNIQUE INDEX IF NOT EXISTS one_checkin_per_student_session ON events(session_id, student_id) WHERE type='CHECKIN';
        CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
        """)
        c.executemany("INSERT OR IGNORE INTO users(id,name,role) VALUES(?,?,?)", [
            ("instructor-1", "Demo Instructor", "instructor"),
            ("student-1", "Demo Student", "student"),
            ("student-2", "Demo Student Two", "student"),
        ])


def now() -> datetime:
    return datetime.now(timezone.utc)


def actor(x_user_id: str | None) -> sqlite3.Row:
    if not x_user_id:
        raise HTTPException(401, "Send X-User-ID")
    with db() as c:
        user = c.execute("SELECT * FROM users WHERE id=?", (x_user_id,)).fetchone()
    if not user:
        raise HTTPException(401, "Unknown user")
    return user


def get_session(c: sqlite3.Connection, sid: str) -> sqlite3.Row:
    row = c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if not row:
        raise HTTPException(404, "Session not found")
    return row


def event(c: sqlite3.Connection, sid: str, student_id: str, kind: str, payload: dict[str, Any], who: str) -> sqlite3.Row:
    # Serialize appends so concurrent requests cannot fork a session's chain.
    c.execute("BEGIN IMMEDIATE")
    last = c.execute("SELECT hash FROM events WHERE session_id=? ORDER BY rowid DESC LIMIT 1", (sid,)).fetchone()
    prev = last["hash"] if last else "0" * 64
    eid, created = str(uuid.uuid4()), now().isoformat()
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    # The canonical event fields and previous hash are included in the digest.
    body = json.dumps({"id": eid, "session_id": sid, "student_id": student_id, "type": kind,
                       "payload": json.loads(encoded), "actor_id": who, "created_at": created,
                       "prev_hash": prev}, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256((prev + body).encode()).hexdigest()
    c.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)", (eid, sid, student_id, kind, encoded, who, created, prev, digest))
    return c.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()


class SessionIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    starts_at: datetime
    ends_at: datetime


class CheckinIn(BaseModel):
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class DisputeIn(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class ResolveIn(BaseModel):
    student_id: str
    decision: str = Field(min_length=1, max_length=1000)
    present: bool


class OverrideIn(BaseModel):
    student_id: str
    present: bool
    reason: str = Field(min_length=1, max_length=1000)


class ReceiptVerifyIn(BaseModel):
    receipt_id: str
    signature: str = Field(min_length=64, max_length=64)


def current_code(secret: str, bucket: int) -> str:
    raw = hmac.new(bytes.fromhex(secret), str(bucket).encode(), hashlib.sha256).digest()
    return f"{int.from_bytes(raw[:4], 'big') % 1_000_000:06d}"


def attendance_state(events: list[sqlite3.Row]) -> dict[str, dict[str, Any]]:
    state: dict[str, dict[str, Any]] = {}
    for e in events:
        item = state.setdefault(e["student_id"], {"status": "absent", "disputed": False, "open_disputes": []})
        p = json.loads(e["payload"])
        if e["type"] == "CHECKIN":
            item["status"] = "present"
        elif e["type"] == "OVERRIDE":
            item["status"] = "present" if p["present"] else "absent"
        elif e["type"] == "DISPUTE":
            item["open_disputes"].append(e["id"])
        elif e["type"] == "RESOLVE":
            item["status"] = "present" if p["present"] else "absent"
            item["open_disputes"] = [d for d in item["open_disputes"] if d != p["dispute_id"]]
        item["disputed"] = bool(item["open_disputes"])
    return state


def verify_event_chain(rows: list[sqlite3.Row]) -> dict[str, Any]:
    prev = "0" * 64
    for index, e in enumerate(rows, start=1):
        body = json.dumps({"id": e["id"], "session_id": e["session_id"], "student_id": e["student_id"], "type": e["type"], "payload": json.loads(e["payload"]), "actor_id": e["actor_id"], "created_at": e["created_at"], "prev_hash": e["prev_hash"]}, sort_keys=True, separators=(",", ":"))
        expected = hashlib.sha256((prev + body).encode()).hexdigest()
        if e["prev_hash"] != prev or not hmac.compare_digest(e["hash"], expected):
            return {"valid": False, "first_invalid_event_id": e["id"], "events_checked": index}
        prev = e["hash"]
    return {"valid": True, "events_checked": len(rows), "head_hash": prev}


@app.post("/sessions", status_code=201)
def create_session(body: SessionIn, x_user_id: str | None = Header(default=None)):
    user = actor(x_user_id)
    if user["role"] != "instructor":
        raise HTTPException(403, "Instructor role required")
    start, end = body.starts_at, body.ends_at
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise HTTPException(422, "Provide timezone-aware starts_at and ends_at, with end after start")
    sid, secret = str(uuid.uuid4()), secrets.token_hex(32)
    with db() as c:
        c.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?)", (sid, body.title, user["id"], secret, start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()))
    return {"id": sid, "title": body.title, "starts_at": start, "ends_at": end}


@app.get("/sessions/{sid}/code")
def session_code(sid: str, x_user_id: str | None = Header(default=None)):
    user = actor(x_user_id)
    with db() as c:
        s = get_session(c, sid)
    if user["id"] != s["instructor_id"]:
        raise HTTPException(403, "Only the session instructor can retrieve the projected code")
    timestamp = int(time.time())
    return {"code": current_code(s["secret"], timestamp // CODE_PERIOD), "valid_for_seconds": CODE_PERIOD - timestamp % CODE_PERIOD}


@app.post("/sessions/{sid}/checkin", status_code=201)
def checkin(sid: str, body: CheckinIn, x_user_id: str | None = Header(default=None)):
    user = actor(x_user_id)
    if user["role"] != "student":
        raise HTTPException(403, "Student role required")
    with db() as c:
        s = get_session(c, sid)
        stamp = now()
        if not (datetime.fromisoformat(s["starts_at"]) <= stamp <= datetime.fromisoformat(s["ends_at"])):
            raise HTTPException(409, "Session is not open")
        bucket = int(stamp.timestamp()) // CODE_PERIOD
        # Accept only the current time bucket, so codes expire at rotation.
        if not hmac.compare_digest(body.code, current_code(s["secret"], bucket)):
            raise HTTPException(400, "Invalid or expired code")
        existing = c.execute("SELECT id FROM events WHERE session_id=? AND student_id=? AND type='CHECKIN'", (sid, user["id"])).fetchone()
        if existing:
            raise HTTPException(409, "A check-in already exists for this student and session")
        try:
            e = event(c, sid, user["id"], "CHECKIN", {"method": "rotating_code"}, user["id"])
        except sqlite3.IntegrityError:
            raise HTTPException(409, "A check-in already exists for this student and session")
        receipt_body = f"{e['id']}|{sid}|{user['id']}|{e['created_at']}|{e['hash']}"
        signature = hmac.new(RECEIPT_KEY, receipt_body.encode(), hashlib.sha256).hexdigest()
        return {"receipt_id": e["id"], "session_id": sid, "student_id": user["id"], "checked_in_at": e["created_at"], "event_hash": e["hash"], "signature": signature}


@app.post("/sessions/{sid}/disputes", status_code=201)
def dispute(sid: str, body: DisputeIn, x_user_id: str | None = Header(default=None)):
    user = actor(x_user_id)
    if user["role"] != "student":
        raise HTTPException(403, "Student role required")
    with db() as c:
        get_session(c, sid)
        open_dispute = c.execute("SELECT 1 FROM events e WHERE e.session_id=? AND e.student_id=? AND e.type='DISPUTE' AND NOT EXISTS (SELECT 1 FROM events r WHERE r.type='RESOLVE' AND json_extract(r.payload,'$.dispute_id')=e.id)", (sid, user["id"])).fetchone()
        if open_dispute:
            raise HTTPException(409, "You already have an open dispute for this session")
        e = event(c, sid, user["id"], "DISPUTE", {"reason": body.reason}, user["id"])
        return {"dispute_id": e["id"], "created_at": e["created_at"], "hash": e["hash"]}


@app.post("/disputes/{dispute_id}/resolve", status_code=201)
def resolve(dispute_id: str, body: ResolveIn, x_user_id: str | None = Header(default=None)):
    user = actor(x_user_id)
    if user["role"] != "instructor":
        raise HTTPException(403, "Instructor role required")
    with db() as c:
        original = c.execute("SELECT * FROM events WHERE id=? AND type='DISPUTE'", (dispute_id,)).fetchone()
        if not original:
            raise HTTPException(404, "Dispute not found")
        s = get_session(c, original["session_id"])
        if s["instructor_id"] != user["id"]:
            raise HTTPException(403, "Only the session instructor can resolve this dispute")
        if original["student_id"] != body.student_id:
            raise HTTPException(422, "student_id must match the disputed student")
        if c.execute("SELECT 1 FROM events WHERE type='RESOLVE' AND json_extract(payload,'$.dispute_id')=?", (dispute_id,)).fetchone():
            raise HTTPException(409, "Dispute has already been resolved")
        e = event(c, s["id"], body.student_id, "RESOLVE", {"dispute_id": dispute_id, "decision": body.decision, "present": body.present}, user["id"])
        return {"resolution_id": e["id"], "created_at": e["created_at"], "hash": e["hash"]}


@app.post("/sessions/{sid}/attendance/override", status_code=201)
def override(sid: str, body: OverrideIn, x_user_id: str | None = Header(default=None)):
    user = actor(x_user_id)
    if user["role"] != "instructor":
        raise HTTPException(403, "Instructor role required")
    with db() as c:
        s = get_session(c, sid)
        if s["instructor_id"] != user["id"]:
            raise HTTPException(403, "Only the session instructor can override attendance")
        if not c.execute("SELECT 1 FROM users WHERE id=? AND role='student'", (body.student_id,)).fetchone():
            raise HTTPException(404, "Student not found")
        e = event(c, sid, body.student_id, "OVERRIDE", {"present": body.present, "reason": body.reason}, user["id"])
        return {"event_id": e["id"], "created_at": e["created_at"], "hash": e["hash"]}


@app.get("/sessions/{sid}/attendance")
def attendance(sid: str, x_user_id: str | None = Header(default=None)):
    user = actor(x_user_id)
    with db() as c:
        s = get_session(c, sid)
        if user["role"] != "instructor" and user["id"] not in {r[0] for r in c.execute("SELECT student_id FROM events WHERE session_id=?", (sid,)).fetchall()}:
            raise HTTPException(403, "No attendance access")
        events = c.execute("SELECT * FROM events WHERE session_id=? ORDER BY rowid", (sid,)).fetchall()
        states = attendance_state(events)
        if user["role"] == "instructor":
            visible_states = states
            visible_events = events
        else:
            visible_states = {user["id"]: states.get(user["id"], {"status": "absent", "disputed": False, "open_disputes": []})}
            visible_events = [e for e in events if e["student_id"] == user["id"]]
        return {"session_id": sid, "attendance": [{"student_id": uid, **value} for uid, value in visible_states.items()],
                "events": [{"id": e["id"], "student_id": e["student_id"], "type": e["type"], "payload": json.loads(e["payload"]), "actor_id": e["actor_id"], "created_at": e["created_at"], "hash": e["hash"]} for e in visible_events]}


@app.get("/sessions/{sid}/verify")
def verify(sid: str, x_user_id: str | None = Header(default=None)):
    user = actor(x_user_id)
    with db() as c:
        s = get_session(c, sid)
        if user["role"] != "instructor" and user["id"] != s["instructor_id"]:
            # Students can verify the chain for a session in which they have an event.
            if not c.execute("SELECT 1 FROM events WHERE session_id=? AND student_id=?", (sid, user["id"])).fetchone():
                raise HTTPException(403, "No session access")
        rows = c.execute("SELECT * FROM events WHERE session_id=? ORDER BY rowid", (sid,)).fetchall()
    return verify_event_chain(rows)


@app.post("/receipts/verify")
def verify_receipt(body: ReceiptVerifyIn, x_user_id: str | None = Header(default=None)):
    user = actor(x_user_id)
    with db() as c:
        e = c.execute("SELECT * FROM events WHERE id=? AND type='CHECKIN'", (body.receipt_id,)).fetchone()
        if not e or e["student_id"] != user["id"]:
            raise HTTPException(404, "Receipt not found")
        rows = c.execute("SELECT * FROM events WHERE session_id=? ORDER BY rowid", (e["session_id"],)).fetchall()
    expected = hmac.new(RECEIPT_KEY, f"{e['id']}|{e['session_id']}|{e['student_id']}|{e['created_at']}|{e['hash']}".encode(), hashlib.sha256).hexdigest()
    chain = verify_event_chain(rows)
    return {"valid": hmac.compare_digest(body.signature, expected) and chain["valid"], "receipt_id": e["id"], "chain_valid": chain["valid"]}


@app.get("/health")
def health():
    return {"ok": True}
