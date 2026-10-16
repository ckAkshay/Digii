from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from typing import Any


def canonical_event_body(*, id: str, session_id: str, student_id: str, type: str, payload: dict[str, Any], actor_id: str, created_at: str, prev_hash: str) -> str:
    """The exact JSON fields digested into an event's hash. Shared by append_event
    (which hashes it when writing) and verify_event_chain (which recomputes it when
    checking) so the two can never silently drift apart."""
    return json.dumps({"id": id, "session_id": session_id, "student_id": student_id, "type": type,
                        "payload": payload, "actor_id": actor_id, "created_at": created_at,
                        "prev_hash": prev_hash}, sort_keys=True, separators=(",", ":"))


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
        body = canonical_event_body(id=e["id"], session_id=e["session_id"], student_id=e["student_id"], type=e["type"], payload=json.loads(e["payload"]), actor_id=e["actor_id"], created_at=e["created_at"], prev_hash=e["prev_hash"])
        expected = hashlib.sha256((prev + body).encode()).hexdigest()
        if e["prev_hash"] != prev or not hmac.compare_digest(e["hash"], expected):
            return {"valid": False, "first_invalid_event_id": e["id"], "events_checked": index}
        prev = e["hash"]
    return {"valid": True, "events_checked": len(rows), "head_hash": prev}
