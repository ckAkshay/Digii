from __future__ import annotations

import hmac
import json
import sqlite3
import secrets
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException

from ..chain import attendance_state, verify_event_chain
from ..clock import get_now
from ..config import settings
from ..db import append_event, get_db, get_session
from ..schemas import CheckinIn, OverrideIn, ReceiptVerifyIn, SessionIn
from ..security import current_code, get_current_user, sign_receipt

router = APIRouter()


@router.post("/sessions", status_code=201)
def create_session(body: SessionIn, user: sqlite3.Row = Depends(get_current_user), db: sqlite3.Connection = Depends(get_db)):
    if user["role"] != "instructor":
        raise HTTPException(403, "Instructor role required")
    start, end = body.starts_at, body.ends_at
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise HTTPException(422, "Provide timezone-aware starts_at and ends_at, with end after start")
    sid, secret = str(uuid.uuid4()), secrets.token_hex(32)
    db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?)", (sid, body.title, user["id"], secret, start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()))
    return {"id": sid, "title": body.title, "starts_at": start, "ends_at": end}


@router.get("/sessions/{sid}/code")
def session_code(sid: str, user: sqlite3.Row = Depends(get_current_user), db: sqlite3.Connection = Depends(get_db)):
    s = get_session(db, sid)
    if user["id"] != s["instructor_id"]:
        raise HTTPException(403, "Only the session instructor can retrieve the projected code")
    timestamp = int(time.time())
    return {"code": current_code(s["secret"], timestamp // settings.code_period), "valid_for_seconds": settings.code_period - timestamp % settings.code_period}


@router.post("/sessions/{sid}/checkin", status_code=201)
def checkin(sid: str, body: CheckinIn, user: sqlite3.Row = Depends(get_current_user), db: sqlite3.Connection = Depends(get_db), when: datetime = Depends(get_now)):
    if user["role"] != "student":
        raise HTTPException(403, "Student role required")
    s = get_session(db, sid)
    if not (datetime.fromisoformat(s["starts_at"]) <= when <= datetime.fromisoformat(s["ends_at"])):
        raise HTTPException(409, "Session is not open")
    bucket = int(when.timestamp()) // settings.code_period
    # Accept only the current time bucket, so codes expire at rotation.
    if not hmac.compare_digest(body.code, current_code(s["secret"], bucket)):
        raise HTTPException(400, "Invalid or expired code")
    existing = db.execute("SELECT id FROM events WHERE session_id=? AND student_id=? AND type='CHECKIN'", (sid, user["id"])).fetchone()
    if existing:
        raise HTTPException(409, "A check-in already exists for this student and session")
    try:
        e = append_event(db, sid, user["id"], "CHECKIN", {"method": "rotating_code"}, user["id"], when)
    except sqlite3.IntegrityError:
        raise HTTPException(409, "A check-in already exists for this student and session")
    return {"receipt_id": e["id"], "session_id": sid, "student_id": user["id"], "checked_in_at": e["created_at"], "event_hash": e["hash"], "signature": sign_receipt(e)}


@router.post("/sessions/{sid}/attendance/override", status_code=201)
def override(sid: str, body: OverrideIn, user: sqlite3.Row = Depends(get_current_user), db: sqlite3.Connection = Depends(get_db), when: datetime = Depends(get_now)):
    if user["role"] != "instructor":
        raise HTTPException(403, "Instructor role required")
    s = get_session(db, sid)
    if s["instructor_id"] != user["id"]:
        raise HTTPException(403, "Only the session instructor can override attendance")
    if not db.execute("SELECT 1 FROM users WHERE id=? AND role='student'", (body.student_id,)).fetchone():
        raise HTTPException(404, "Student not found")
    e = append_event(db, sid, body.student_id, "OVERRIDE", {"present": body.present, "reason": body.reason}, user["id"], when)
    return {"event_id": e["id"], "created_at": e["created_at"], "hash": e["hash"]}


@router.get("/sessions/{sid}/attendance")
def attendance(sid: str, user: sqlite3.Row = Depends(get_current_user), db: sqlite3.Connection = Depends(get_db)):
    s = get_session(db, sid)
    if user["role"] != "instructor" and user["id"] not in {r[0] for r in db.execute("SELECT student_id FROM events WHERE session_id=?", (sid,)).fetchall()}:
        raise HTTPException(403, "No attendance access")
    events = db.execute("SELECT * FROM events WHERE session_id=? ORDER BY rowid", (sid,)).fetchall()
    states = attendance_state(events)
    if user["role"] == "instructor":
        visible_states = states
        visible_events = events
    else:
        visible_states = {user["id"]: states.get(user["id"], {"status": "absent", "disputed": False, "open_disputes": []})}
        visible_events = [e for e in events if e["student_id"] == user["id"]]
    return {"session_id": sid, "attendance": [{"student_id": uid, **value} for uid, value in visible_states.items()],
            "events": [{"id": e["id"], "student_id": e["student_id"], "type": e["type"], "payload": json.loads(e["payload"]), "actor_id": e["actor_id"], "created_at": e["created_at"], "hash": e["hash"]} for e in visible_events]}


@router.get("/sessions/{sid}/verify")
def verify(sid: str, user: sqlite3.Row = Depends(get_current_user), db: sqlite3.Connection = Depends(get_db)):
    s = get_session(db, sid)
    if user["role"] != "instructor" and user["id"] != s["instructor_id"]:
        # Students can verify the chain for a session in which they have an event.
        if not db.execute("SELECT 1 FROM events WHERE session_id=? AND student_id=?", (sid, user["id"])).fetchone():
            raise HTTPException(403, "No session access")
    rows = db.execute("SELECT * FROM events WHERE session_id=? ORDER BY rowid", (sid,)).fetchall()
    return verify_event_chain(rows)


@router.post("/receipts/verify")
def verify_receipt(body: ReceiptVerifyIn, user: sqlite3.Row = Depends(get_current_user), db: sqlite3.Connection = Depends(get_db)):
    e = db.execute("SELECT * FROM events WHERE id=? AND type='CHECKIN'", (body.receipt_id,)).fetchone()
    if not e or e["student_id"] != user["id"]:
        raise HTTPException(404, "Receipt not found")
    rows = db.execute("SELECT * FROM events WHERE session_id=? ORDER BY rowid", (e["session_id"],)).fetchall()
    expected = sign_receipt(e)
    chain = verify_event_chain(rows)
    return {"valid": hmac.compare_digest(body.signature, expected) and chain["valid"], "receipt_id": e["id"], "chain_valid": chain["valid"]}
