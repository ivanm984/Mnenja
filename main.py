from app.logging_config import setup_logging

setup_logging()

from app.routes import app

__all__ = ["app"]
