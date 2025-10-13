"""Pydantic request/response models."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SaveSessionPayload(BaseModel):
    session_id: str = Field(..., min_length=1)
    data: Dict[str, Any]
    project_name: Optional[str] = None
    summary: Optional[str] = None


class ConfirmReportPayload(BaseModel):
    session_id: str = Field(..., min_length=1)
    excluded_ids: Optional[List[str]] = None


__all__ = ["SaveSessionPayload", "ConfirmReportPayload"]
