from __future__ import annotations

import hmac
import hashlib
import sqlite3

from fastapi import Depends, Header, HTTPException

from .config import settings
from .db import get_db


def get_current_user(x_user_id: str | None = Header(default=None), db: sqlite3.Connection = Depends(get_db)) -> sqlite3.Row:
    if not x_user_id:
        raise HTTPException(401, "Send X-User-ID")
    user = db.execute("SELECT * FROM users WHERE id=?", (x_user_id,)).fetchone()
    if not user:
        raise HTTPException(401, "Unknown user")
    return user


def current_code(secret: str, bucket: int) -> str:
    raw = hmac.new(bytes.fromhex(secret), str(bucket).encode(), hashlib.sha256).digest()
    return f"{int.from_bytes(raw[:4], 'big') % 1_000_000:06d}"


def sign_receipt(event: sqlite3.Row) -> str:
    body = f"{event['id']}|{event['session_id']}|{event['student_id']}|{event['created_at']}|{event['hash']}"
    return hmac.new(settings.receipt_key, body.encode(), hashlib.sha256).hexdigest()
