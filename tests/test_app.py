import json
import time
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import main


def setup_client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "test.db")
    main.init_db()
    return TestClient(main.app)


def open_session(client):
    start = datetime.now(timezone.utc) - timedelta(minutes=1)
    end = start + timedelta(hours=2)
    return client.post("/sessions", headers={"X-User-ID": "instructor-1"}, json={
        "title": "Lab", "starts_at": start.isoformat(), "ends_at": end.isoformat()
    }).json()["id"]


def test_root_serves_product_interface(tmp_path, monkeypatch):
    client = setup_client(tmp_path, monkeypatch)
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "A clearer record" in response.text
    assert "Check in" in response.text
    assert "attendance-user" in response.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200


def test_code_rotates_by_time_bucket():
    secret = "01" * 32
    assert main.current_code(secret, 10) == main.current_code(secret, 10)
    assert main.current_code(secret, 10) != main.current_code(secret, 11)


def test_checkin_receipt_duplicate_and_expired_code(tmp_path, monkeypatch):
    client = setup_client(tmp_path, monkeypatch)
    sid = open_session(client)
    code = client.get(f"/sessions/{sid}/code", headers={"X-User-ID": "instructor-1"}).json()["code"]
    headers = {"X-User-ID": "student-1"}
    result = client.post(f"/sessions/{sid}/checkin", headers=headers, json={"code": code})
    assert result.status_code == 201
    assert result.json()["signature"]
    receipt = result.json()
    verified = client.post("/receipts/verify", headers=headers, json={"receipt_id": receipt["receipt_id"], "signature": receipt["signature"]})
    assert verified.json()["valid"] is True
    bad_signature = client.post("/receipts/verify", headers=headers, json={"receipt_id": receipt["receipt_id"], "signature": "0" * 64})
    assert bad_signature.json()["valid"] is False
    duplicate = client.post(f"/sessions/{sid}/checkin", headers=headers, json={"code": code})
    assert duplicate.status_code == 409
    real_time = time.time
    monkeypatch.setattr(main, "now", lambda: datetime.fromtimestamp((int(real_time()) // 30 + 2) * 30, timezone.utc))
    replay = client.post(f"/sessions/{sid}/checkin", headers={"X-User-ID": "student-2"}, json={"code": code})
    assert replay.status_code == 400


def test_event_chain_detects_payload_tampering(tmp_path, monkeypatch):
    client = setup_client(tmp_path, monkeypatch)
    sid = open_session(client)
    code = client.get(f"/sessions/{sid}/code", headers={"X-User-ID": "instructor-1"}).json()["code"]
    client.post(f"/sessions/{sid}/checkin", headers={"X-User-ID": "student-1"}, json={"code": code})
    assert client.get(f"/sessions/{sid}/verify", headers={"X-User-ID": "student-1"}).json()["valid"] is True
    with main.db() as c:
        c.execute("DROP TRIGGER events_no_update")
        c.execute("UPDATE events SET payload=? WHERE session_id=?", (json.dumps({"method": "tampered"}), sid))
    response = client.get(f"/sessions/{sid}/verify", headers={"X-User-ID": "student-1"}).json()
    assert response["valid"] is False


def test_dispute_resolution_and_override_are_audited(tmp_path, monkeypatch):
    client = setup_client(tmp_path, monkeypatch)
    sid = open_session(client)
    student = {"X-User-ID": "student-1"}
    instructor = {"X-User-ID": "instructor-1"}
    opened = client.post(f"/sessions/{sid}/disputes", headers=student, json={"reason": "I was present"})
    assert opened.status_code == 201
    duplicate = client.post(f"/sessions/{sid}/disputes", headers=student, json={"reason": "Still disputing"})
    assert duplicate.status_code == 409
    resolved = client.post(f"/disputes/{opened.json()['dispute_id']}/resolve", headers=instructor,
                           json={"student_id": "student-1", "decision": "Confirmed present", "present": True})
    assert resolved.status_code == 201
    override = client.post(f"/sessions/{sid}/attendance/override", headers=instructor,
                           json={"student_id": "student-2", "present": True, "reason": "Roster correction"})
    assert override.status_code == 201
    data = client.get(f"/sessions/{sid}/attendance", headers=instructor).json()
    assert {x["student_id"]: x["status"] for x in data["attendance"]} == {"student-1": "present", "student-2": "present"}
    assert [e["type"] for e in data["events"]] == ["DISPUTE", "RESOLVE", "OVERRIDE"]
    assert client.get(f"/sessions/{sid}/verify", headers=instructor).json()["valid"] is True


def test_event_rows_are_append_only_and_student_attendance_is_private(tmp_path, monkeypatch):
    client = setup_client(tmp_path, monkeypatch)
    sid = open_session(client)
    instructor = {"X-User-ID": "instructor-1"}
    client.post(f"/sessions/{sid}/disputes", headers={"X-User-ID": "student-1"}, json={"reason": "Please confirm my record"})
    client.post(f"/sessions/{sid}/attendance/override", headers=instructor,
                json={"student_id": "student-2", "present": True, "reason": "Correction"})
    mine = client.get(f"/sessions/{sid}/attendance", headers={"X-User-ID": "student-1"})
    assert mine.status_code == 200
    assert all(item["student_id"] == "student-1" for item in mine.json()["attendance"])
    assert all(item["student_id"] == "student-1" for item in mine.json()["events"])
    with main.db() as c:
        try:
            c.execute("UPDATE events SET payload='{}' WHERE session_id=?", (sid,))
            assert False, "Database trigger should reject event updates"
        except Exception as exc:
            assert "append-only" in str(exc)
