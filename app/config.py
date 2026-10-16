from __future__ import annotations

import os
from pathlib import Path


class Settings:
    def __init__(self) -> None:
        self.db_path = Path(os.getenv("ATTENDANCE_DB", "attendance.db"))
        self.receipt_key = os.getenv("RECEIPT_KEY", "development-receipt-key-change-me").encode()
        self.code_period = 30


settings = Settings()
