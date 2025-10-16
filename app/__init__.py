from fastapi import FastAPI
from .routes import frontend_router

# Create the FastAPI app instance
app = FastAPI(title="Skladnost App")

# Include the router from routes.py
app.include_router(frontend_router)

__all__ = ["app"]