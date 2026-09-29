from __future__ import annotations

"""
Backward-compatibility facade for api.py.
Points directly to the decoupled app.main:app.
"""

from app.main import app, create_app
from app.schemas.schedule import (
    MonthlyScheduleRequest,
)

__all__ = [
    "app",
    "create_app",
    "MonthlyScheduleRequest",
]

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="127.0.0.1", port=8000, reload=True)
