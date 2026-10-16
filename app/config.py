from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("digii.attendance")

_DEFAULT_RECEIPT_KEY = "development-receipt-key-change-me"


class Settings:
    def __init__(self) -> None:
        self.db_path = Path(os.getenv("ATTENDANCE_DB", "attendance.db"))
        receipt_key = os.getenv("RECEIPT_KEY", _DEFAULT_RECEIPT_KEY)
        if receipt_key == _DEFAULT_RECEIPT_KEY:
            logger.warning(
                "RECEIPT_KEY is not set; using the public, well-known development key. "
                "Receipts signed with it can be forged by anyone who reads this source. "
                "Set RECEIPT_KEY to a private, stable secret before real use."
            )
        self.receipt_key = receipt_key.encode()
        self.code_period = 30


settings = Settings()
