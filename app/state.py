"""In-memory runtime state containers."""
from __future__ import annotations

from typing import Any, Dict, Optional


TEMP_STORAGE: Dict[str, Dict[str, Any]] = {}
LAST_DOCX_PATH: Optional[str] = None
LAST_XLSX_PATH: Optional[str] = None
LATEST_REPORT_CACHE: Dict[str, Dict[str, Any]] = {}

__all__ = ["TEMP_STORAGE", "LAST_DOCX_PATH", "LAST_XLSX_PATH", "LATEST_REPORT_CACHE"]
