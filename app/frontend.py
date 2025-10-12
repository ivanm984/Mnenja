"""Frontend HTML helper."""
from __future__ import annotations

from datetime import datetime

from .config import PROJECT_ROOT

FRONTEND_PATH = PROJECT_ROOT / "app" / "frontend.html"


def build_homepage() -> str:
    html = FRONTEND_PATH.read_text(encoding="utf-8")
    return html.replace("YEAR_PLACEHOLDER", str(datetime.now().year))


__all__ = ["build_homepage"]
