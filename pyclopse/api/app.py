"""FastAPI application for pyclopse."""
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
from .routes import reflect as reflect_routes
from .routes import skills as skills_routes
from .routes import events as events_routes
from .routes import commands as commands_routes
from .routes import tui as tui_routes

logger = logging.getLogger("pyclopse.api")


# Global gateway reference (set by create_app)
_gateway: Optional[Any] = None

# Global app reference (set by create_app)
_app: Optional[Any] = None


def set_gateway(gateway: Any) -> None:
    """Set the global gateway instance for API access.

    Args:
        gateway (Any): The gateway instance to register.
    """
    global _gateway
    _gateway = gateway


def get_gateway() -> Any:
    """Get the global gateway instance.

    Returns:
        Any: The registered gateway instance.

    Raises:
        HTTPException: With status 503 if the gateway has not been initialized.
    """
    if _gateway is None:
        raise HTTPException(status_code=503, detail="Gateway not initialized")
    return _gateway


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage the FastAPI application lifespan.

    Logs startup and shutdown events. Used as the lifespan context manager
    for the FastAPI application.

    Args:
        app (FastAPI): The FastAPI application instance.

    Yields:
        None: Control is yielded to the application for its normal lifecycle.
    """
    logger.info("Starting pyclopse API server...")
    yield
    logger.info("Shutting down pyclopse API server...")


def create_app(gateway: Optional[Any] = None) -> FastAPI:
    """Create and configure the FastAPI application.

    Loads configuration, sets up CORS middleware, registers all API routers
    under ``/api/v1/``, and installs a global exception handler.

    Args:
        gateway (Optional[Any]): Gateway instance to bind to the app.
            When provided, ``set_gateway()`` is called immediately so all
            route handlers can reach the gateway via ``get_gateway()``.

    Returns:
        FastAPI: The fully configured application instance.
    """
    from pyclopse.config.loader import ConfigLoader
    
    # Get config
    config_loader = ConfigLoader()
    config = config_loader.load()
    
    app = FastAPI(
        title="pyclopse API",
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

    global _app
    _app = app

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
    app.include_router(reflect_routes.router, prefix="/api/v1/reflect", tags=["reflect"])
    app.include_router(skills_routes.router, prefix="/api/v1/skills", tags=["skills"])
    app.include_router(events_routes.router, prefix="/api/v1", tags=["events"])
    app.include_router(commands_routes.router, prefix="/api/v1/commands", tags=["commands"])
    app.include_router(tui_routes.router, prefix="/api/v1/tui", tags=["tui"])

    # Health check
    @app.get("/health")
    async def health_check():
        """Return a simple liveness probe response.

        Returns:
            dict: ``{"status": "healthy", "service": "pyclopse"}``.
        """
        return {"status": "healthy", "service": "pyclopse"}

    @app.get("/")
    async def root():
        """Return service discovery metadata.

        Returns:
            dict: Service name, version, and link to the interactive docs.
        """
        return {
            "service": "pyclopse",
            "version": "0.1.0",
            "docs": "/docs",
        }

    # Error handler
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        """Handle any unhandled exception and return a generic 500 response.

        Args:
            request (Request): The incoming HTTP request.
            exc (Exception): The unhandled exception.

        Returns:
            JSONResponse: HTTP 500 with ``{"detail": "Internal server error"}``.
        """
        logger.error(f"Unhandled exception: {exc}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )
    
    return app
