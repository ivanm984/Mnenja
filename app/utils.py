"""Miscellaneous helpers."""
from __future__ import annotations

from typing import Any, Dict


def infer_project_name(data: Dict[str, Any], fallback: str = "Neimenovan projekt") -> str:
    candidates = []
    for key in ("metadata", "keyData"):
        section = data.get(key)
        if isinstance(section, dict):
            name = section.get("ime_projekta") or section.get("ime_projekta_original")
            if name:
                candidates.append(str(name))
    direct_name = data.get("projectName") or data.get("project_name")
    if direct_name:
        candidates.insert(0, str(direct_name))
    for value in candidates:
        clean = value.strip()
        if clean:
            return clean
    return fallback


__all__ = ["infer_project_name"]
