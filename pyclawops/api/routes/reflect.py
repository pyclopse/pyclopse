"""REST API routes for the pyclawops reflection system.

Mirrors the reflect() and reflect_source() MCP tools over HTTP for external
clients that cannot connect to MCP directly.

Endpoints:
  GET /api/v1/reflect                          — architecture overview
  GET /api/v1/reflect?category=system          — list all systems
  GET /api/v1/reflect?category=system&name=X   — system X detail
  GET /api/v1/reflect/source/{module}          — read source with line numbers
"""

import logging
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse

logger = logging.getLogger("pyclawops.api.reflect")
router = APIRouter()


@router.get("", response_class=PlainTextResponse)
async def reflect(
    category: str = Query(default=""),
    name: str = Query(default=""),
):
    """Query the pyclawops live architecture reflection registry."""
    from pyclawops.reflect import query as _query
    return _query(
        category=category if category else None,
        name=name if name else None,
    )


@router.get("/source/{module_path:path}", response_class=PlainTextResponse)
async def reflect_source(module_path: str):
    """Read pyclawops source code with line numbers.

    module_path examples: 'core/gateway.py', 'agents/runner.py'
    Only paths within the pyclawops package are accessible.
    """
    from pyclawops.reflect import source_file
    result = source_file(module_path)
    if result.startswith("[NOT FOUND]"):
        raise HTTPException(status_code=404, detail=result)
    if result.startswith("[ERROR]"):
        raise HTTPException(status_code=400, detail=result)
    return result
