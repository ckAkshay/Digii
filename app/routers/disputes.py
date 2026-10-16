from __future__ import annotations

import sqlite3
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from ..clock import get_now
from ..db import append_event, get_db, get_session
from ..schemas import DisputeIn, ResolveIn
from ..security import get_current_user

router = APIRouter()


@router.post("/sessions/{sid}/disputes", status_code=201)
def dispute(sid: str, body: DisputeIn, user: sqlite3.Row = Depends(get_current_user), db: sqlite3.Connection = Depends(get_db), when: datetime = Depends(get_now)):
    if user["role"] != "student":
        raise HTTPException(403, "Student role required")
    get_session(db, sid)
    open_dispute = db.execute("SELECT 1 FROM events e WHERE e.session_id=? AND e.student_id=? AND e.type='DISPUTE' AND NOT EXISTS (SELECT 1 FROM events r WHERE r.type='RESOLVE' AND json_extract(r.payload,'$.dispute_id')=e.id)", (sid, user["id"])).fetchone()
    if open_dispute:
        raise HTTPException(409, "You already have an open dispute for this session")
    e = append_event(db, sid, user["id"], "DISPUTE", {"reason": body.reason}, user["id"], when)
    return {"dispute_id": e["id"], "created_at": e["created_at"], "hash": e["hash"]}


@router.post("/disputes/{dispute_id}/resolve", status_code=201)
def resolve(dispute_id: str, body: ResolveIn, user: sqlite3.Row = Depends(get_current_user), db: sqlite3.Connection = Depends(get_db), when: datetime = Depends(get_now)):
    if user["role"] != "instructor":
        raise HTTPException(403, "Instructor role required")
    original = db.execute("SELECT * FROM events WHERE id=? AND type='DISPUTE'", (dispute_id,)).fetchone()
    if not original:
        raise HTTPException(404, "Dispute not found")
    s = get_session(db, original["session_id"])
    if s["instructor_id"] != user["id"]:
        raise HTTPException(403, "Only the session instructor can resolve this dispute")
    if original["student_id"] != body.student_id:
        raise HTTPException(422, "student_id must match the disputed student")
    if db.execute("SELECT 1 FROM events WHERE type='RESOLVE' AND json_extract(payload,'$.dispute_id')=?", (dispute_id,)).fetchone():
        raise HTTPException(409, "Dispute has already been resolved")
    e = append_event(db, s["id"], body.student_id, "RESOLVE", {"dispute_id": dispute_id, "decision": body.decision, "present": body.present}, user["id"], when)
    return {"resolution_id": e["id"], "created_at": e["created_at"], "hash": e["hash"]}
