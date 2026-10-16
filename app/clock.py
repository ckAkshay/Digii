from __future__ import annotations

from datetime import datetime, timezone


def get_now() -> datetime:
    """FastAPI dependency; tests override it via app.dependency_overrides."""
    return datetime.now(timezone.utc)
