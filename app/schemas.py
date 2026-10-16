from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


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
