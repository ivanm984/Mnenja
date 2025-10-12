"""File-system helpers for storing uploaded revisions."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Tuple

from .config import DATA_DIR

REVISION_ROOT = DATA_DIR / "revisions"
REVISION_ROOT.mkdir(parents=True, exist_ok=True)

SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def sanitize_filename(filename: str) -> str:
    if not filename:
        return "datoteka.pdf"
    name = SAFE_NAME_RE.sub("_", filename)
    return name.strip("._") or "datoteka.pdf"


def save_revision_files(
    session_id: str,
    files: Iterable[Tuple[str, bytes, str]],
    requirement_id: str | None = None,
) -> Tuple[List[str], List[str], List[str]]:
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    folder_parts = [session_id, requirement_id or "full"]
    target_dir = REVISION_ROOT.joinpath(*folder_parts)
    target_dir.mkdir(parents=True, exist_ok=True)

    filenames: List[str] = []
    file_paths: List[str] = []
    mime_types: List[str] = []

    for original_name, content, mime in files:
        safe_name = sanitize_filename(original_name)
        stored_name = f"{timestamp}_{safe_name}"
        destination = target_dir / stored_name
        destination.write_bytes(content)
        filenames.append(original_name or safe_name)
        file_paths.append(str(destination.relative_to(DATA_DIR)))
        mime_types.append(mime or "application/octet-stream")
    return filenames, file_paths, mime_types


__all__ = ["save_revision_files"]
