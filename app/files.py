"""File-system helpers for storing uploaded revisions."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Iterable, List, Tuple

from app.logging_config import get_logger

from .config import DATA_DIR

logger = get_logger(__name__)

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
    total_bytes = 0
    start = perf_counter()

    for original_name, content, mime in files:
        safe_name = sanitize_filename(original_name)
        stored_name = f"{timestamp}_{safe_name}"
        destination = target_dir / stored_name
        destination.write_bytes(content)
        filenames.append(original_name or safe_name)
        file_paths.append(str(destination.relative_to(DATA_DIR)))
        mime_types.append(mime or "application/octet-stream")
        total_bytes += len(content or b"")
        logger.debug(
            "save_revision_files: stored %s (%d bytes) -> %s",
            original_name or safe_name,
            len(content or b""),
            destination,
        )

    duration = perf_counter() - start
    logger.info(
        "save_revision_files: saved %d files (bytes=%d, requirement=%s) in %.3fs to %s",
        len(filenames),
        total_bytes,
        requirement_id or "full",
        duration,
        target_dir,
    )
    return filenames, file_paths, mime_types


__all__ = ["save_revision_files"]
