# REST API

**File:** `pyclawops/api/app.py`, `pyclawops/api/routes/`
**Port:** 8080
**Docs:** `http://localhost:8080/docs`

The REST API serves external clients and internal MCP tool callbacks. MCP tools
call the REST API to read/write gateway state — it is the single HTTP interface
to gateway internals.

---

## App Factory

`create_app(gateway?)` creates the FastAPI instance. `get_gateway()` is the
dependency used by all route handlers — reads the module-level `_gateway`
reference. Raises 503 if called before `set_gateway()`.

---

## Routes

### `/api/v1/agents` — Agents
| `GET /` | List configured agents |

### `/api/v1/channels` — Channels
| `GET /` | List active channel adapters |

### `/api/v1/config` — Config
| `GET /` | Config (secrets redacted) |
| `POST /reload` | Reload from disk |
| `PUT /{path}` | Set value by dot-notation path |
| `DELETE /{path}` | Delete value |

### `/api/v1/jobs` — Jobs (see systems/jobs for full reference)

### `/api/v1/sessions` — Sessions
| `GET /` | List (filter: agent_id, channel, active_only) |
| `GET /{id}` | Session + message history |
| `DELETE /{id}` | Remove from index (files kept) |

### `/api/v1/health` — Health
| `GET /` | Simple health check |
| `GET /detail` | Detailed health status |

### `/api/v1/hooks` — Hooks
| `GET /` | List registered hooks |

### `/api/v1/todos` — Todos
| CRUD for todo items |

### `/api/v1/tools` — Tools
| `GET /` | MCP config per agent |
| `GET /debug` | Live FA runner state |

### `/api/v1/usage` — Usage
| `GET /` | Message and token counters |

### `/api/v1/subagents` — Subagents (see systems/jobs)

### `/api/v1/self` — Self Knowledge
| `GET /topics` | List knowledge topics |
| `GET /topic/{path}` | Read a topic |
| `GET /source/{module}` | Read pyclawops source |

---

## Route Pattern

```python
# pyclawops/api/routes/example.py
from fastapi import APIRouter
router = APIRouter()

def _get_gateway():
    from pyclawops.api.app import get_gateway
    return get_gateway()

@router.get("/")
async def get_things():
    gateway = _get_gateway()
    ...
```

`_get_gateway()` uses a deferred import to avoid circular dependencies at import
time. This pattern is used in every route module.

---

## Adding a New Route

Edit `pyclawops/api/app.py`:
```python
from .routes import my_routes
app.include_router(my_routes.router, prefix="/api/v1/my-thing", tags=["my-thing"])
```
