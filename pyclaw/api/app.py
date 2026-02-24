"""FastAPI application for pyclaw."""
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .routes import agents, channels, jobs, nodes

logger = logging.getLogger("pyclaw.api")


# Global gateway reference (set by gateway.py)
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
    app.include_router(jobs.router, prefix="/api/v1/jobs", tags=["jobs"])
    app.include_router(nodes.router, prefix="/api/v1/nodes", tags=["nodes"])
    
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


# Default app instance
app = create_app()
