"""FastAPI application for pyclaw."""
import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .routes import agents, channels, jobs, sessions, todos as todos_routes
from .routes import config as config_routes
from .routes import usage as usage_routes
from .routes import tools as tools_routes
from .routes import health as health_routes
from .routes import hooks as hooks_routes
from .routes import subagents as subagents_routes

logger = logging.getLogger("pyclaw.api")


# Global gateway reference (set by create_app)
_gateway: Optional[Any] = None


def set_gateway(gateway: Any) -> None:
    """Set the global gateway instance for API access."""
    global _gateway
    _gateway = gateway


def get_gateway() -> Any:
    """Get the global gateway instance."""
    if _gateway is None:
        raise HTTPException(status_code=503, detail="Gateway not initialized")
    return _gateway


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    logger.info("Starting pyclaw API server...")
    yield
    logger.info("Shutting down pyclaw API server...")


def create_app(gateway: Optional[Any] = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    from pyclaw.config.loader import ConfigLoader
    
    # Get config
    config_loader = ConfigLoader()
    config = config_loader.load()
    
    app = FastAPI(
        title="pyclaw API",
        description="Python Gateway API for mobile and external clients",
        version="0.1.0",
        lifespan=lifespan,
    )
    
    # Set CORS
    cors_origins = config.gateway.cors_origins if config else ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    # Set gateway
    if gateway:
        set_gateway(gateway)
    
    # Include routers
    app.include_router(agents.router, prefix="/api/v1/agents", tags=["agents"])
    app.include_router(channels.router, prefix="/api/v1/channels", tags=["channels"])
    app.include_router(config_routes.router, prefix="/api/v1/config", tags=["config"])
    app.include_router(jobs.router, prefix="/api/v1/jobs", tags=["jobs"])
    app.include_router(sessions.router, prefix="/api/v1/sessions", tags=["sessions"])
    app.include_router(usage_routes.router, prefix="/api/v1/usage", tags=["usage"])
    app.include_router(tools_routes.router, prefix="/api/v1/tools", tags=["tools"])
    app.include_router(health_routes.router, prefix="/api/v1/health", tags=["health"])
    app.include_router(todos_routes.router, prefix="/api/v1/todos", tags=["todos"])
    app.include_router(hooks_routes.router, prefix="/api/v1/hooks", tags=["hooks"])
    app.include_router(subagents_routes.router, prefix="/api/v1/subagents", tags=["subagents"])

    # Health check
    @app.get("/health")
    async def health_check():
        return {"status": "healthy", "service": "pyclaw"}
    
    @app.get("/")
    async def root():
        return {
            "service": "pyclaw",
            "version": "0.1.0",
            "docs": "/docs",
        }
    
    # Error handler
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.error(f"Unhandled exception: {exc}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )
    
    return app
