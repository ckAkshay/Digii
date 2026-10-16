# Attendance & Participation

A small FastAPI and SQLite prototype for student initiated check-in using a rotating classroom code. Each state change is recorded as an append-only event in a per-session SHA-256 hash chain. The student receives a signed receipt they can retain, while instructors and students can inspect the event history and verify its integrity.

## Run locally

Requires Python 3.11 or newer.

```powershell
# Run these commands from the Digii project folder.
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:RECEIPT_KEY = "replace-this-with-a-long-random-secret"
uvicorn main:app --reload
```

The API documentation is at <http://127.0.0.1:8000/docs>. SQLite creates `attendance.db` in the current directory. Set `ATTENDANCE_DB` to choose another location.

## Demo identities and authentication

This prototype seeds three demo identities on startup. Pass the identity in the `X-User-ID` header:

| ID | Role |
| --- | --- |
| `instructor-1` | instructor |
| `student-1` | student |
| `student-2` | student |

This header is a development convenience, not authentication. A production service must replace it with authenticated identity and authorization claims. `RECEIPT_KEY` must be a private, stable secret in production. The receipt signature here is HMAC-SHA256; `POST /receipts/verify` checks a student's receipt signature and the current event chain. A student can independently verify only through a trusted verifier holding the shared key. Publicly verifiable receipts would use asymmetric signing. The built-in default key is for local demonstration only.

## Typical flow

1. Create a session with `POST /sessions` as `instructor-1`, using timezone-aware ISO 8601 start and end timestamps.
2. Project `GET /sessions/{id}/code` as the instructor. The six digit code changes every 30 seconds; only the current time bucket is accepted.
3. Check in as a student with `POST /sessions/{id}/checkin` and `{"code":"123456"}`. Save the returned receipt.
4. Submit the returned `receipt_id` and `signature` to `POST /receipts/verify`; review `GET /sessions/{id}/attendance` and verify the event chain with `GET /sessions/{id}/verify`.
5. Students may file `POST /sessions/{id}/disputes`. Instructors can resolve a dispute with `POST /disputes/{id}/resolve` and can record a correction with `POST /sessions/{id}/attendance/override` (reason required).

## Design choices and assumptions

- Attendance is derived by replaying events. `CHECKIN` marks present; `OVERRIDE` changes status with a required reason; `DISPUTE` flags the current record; and `RESOLVE` applies a reasoned decision to its named dispute. A dispute stays open until a matching resolution event is recorded. The history remains visible.
- Every event stores its predecessor hash and a hash of the canonical event fields. SQLite triggers reject ordinary updates and deletes; verification detects modified events and missing/reordered events inside the chain. Removing the final event or rewriting the entire database cannot be detected without a previously anchored chain head. Production should periodically anchor signed heads in an independently controlled store.
- The session secret is generated server-side and stored in SQLite for this local prototype. Production should protect secrets at rest and separate the signing key from the database.
- This is a deliberately narrow prototype. Face recognition, GPS/geofencing, LMS integration, and a full UI are omitted because they add privacy and implementation costs for modest gains over a verifiable record. Demo identity headers are used in place of full authentication for the same scope reason.
- A student can still relay a live code to a friend. A shorter acceptance window and per-device limits are possible future mitigations, though they add friction and do not eliminate collusion.
- There is no enrollment model in this prototype. Any seeded student can attempt a check-in during an open session; production should enforce course membership and session capacity/policy.

## Endpoints

- `POST /sessions`
- `GET /sessions/{id}/code`
- `POST /sessions/{id}/checkin`
- `POST /sessions/{id}/disputes`
- `POST /disputes/{id}/resolve`
- `POST /sessions/{id}/attendance/override`
- `GET /sessions/{id}/attendance`
- `GET /sessions/{id}/verify`
- `POST /receipts/verify`

## Tests

```powershell
pytest -q
```

Tests cover code rotation, expired-code replay, duplicate check-ins, receipt signature verification, dispute resolution, reasoned overrides, student privacy, append-only enforcement, and detection of modified event payloads.

