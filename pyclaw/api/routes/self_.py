"""REST API routes for the pyclaw self-knowledge system.

Exposes the same data as the self MCP server (port 8082) over HTTP, for
external clients that cannot connect to MCP directly.

Endpoints:
  GET /api/v1/self/topics            — list all knowledge topics
  GET /api/v1/self/topic/{path}      — read a documentation topic
  GET /api/v1/self/source/{module}   — read pyclaw source with line numbers
"""

import logging
from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

logger = logging.getLogger("pyclaw.api.self")
router = APIRouter()

# Loader is module-level so it's constructed once per process.
_loader = None


def _get_loader():
    """Return the module-level DocLoader, constructing it on first call.

    Returns:
        DocLoader: Shared documentation loader instance.
    """
    global _loader
    if _loader is None:
        from pyclaw.self.loader import DocLoader
        _loader = DocLoader()
    return _loader


@router.get("/topics", response_class=PlainTextResponse)
async def get_topics():
    """List all available pyclaw self-knowledge topics."""
    return _get_loader().topics()


@router.get("/topic/{topic_path:path}", response_class=PlainTextResponse)
async def get_topic(topic_path: str):
    """Read documentation for a specific knowledge topic.

    topic_path examples: 'overview', 'architecture/gateway', 'systems/jobs'
    """
    result = _get_loader().read(topic_path)
    if result.startswith("[NOT FOUND]"):
        raise HTTPException(status_code=404, detail=result)
    if result.startswith("[ERROR]"):
        raise HTTPException(status_code=400, detail=result)
    return result


@router.get("/source/{module_path:path}", response_class=PlainTextResponse)
async def get_source(module_path: str):
    """Read pyclaw source code with line numbers.

    module_path examples: 'core/gateway.py', 'agents/runner.py'
    Only paths within the pyclaw package are accessible.
    """
    result = _get_loader().source(module_path)
    if result.startswith("[NOT FOUND]"):
        raise HTTPException(status_code=404, detail=result)
    if result.startswith("[ERROR]"):
        raise HTTPException(status_code=400, detail=result)
    return result
