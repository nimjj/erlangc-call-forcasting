from app.api.health import router as health_router
from app.api.v1.router import router as v1_router

__all__ = ["health_router", "v1_router"]
