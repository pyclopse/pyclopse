# Extending pyclawops

How to add new channels, hooks, MCP tools, providers, and config sections.

---

## Adding a Channel Adapter

1. Create `pyclawops/channels/myplatform.py` implementing `ChannelPlugin`:

```python
from pyclawops.channels.plugin import ChannelPlugin, GatewayHandle

class MyPlatformPlugin(ChannelPlugin):
    async def start(self, handle: GatewayHandle) -> None:
        self._handle = handle
        # start polling / webhook listener

    async def stop(self) -> None:
        # cleanup

    async def send(self, channel: str, user_id: str, text: str, **kwargs) -> None:
        # deliver text to user_id on this platform
```

2. Register via entry point in `pyproject.toml`:
```toml
[project.entry-points."pyclawops.channels"]
myplatform = "pyclawops.channels.myplatform:MyPlatformPlugin"
```

Or via explicit config:
```yaml
plugins:
  channels:
    - pyclawops.channels.myplatform:MyPlatformPlugin
```

3. Add a config class for the platform in `pyclawops/config/schema.py` and add it
   to `ChannelsConfig`.

---

## Adding an MCP Tool

All tools live in `pyclawops/tools/server.py`. Add a new `@mcp.tool()` function:

```python
@mcp.tool()
def my_new_tool(arg1: str, arg2: int = 10) -> str:
    """One-line description shown to the agent.

    Longer explanation of what this tool does and when to use it.

    Args:
        arg1: Description of arg1
        arg2: Description of arg2 (default: 10)

    Example:
        my_new_tool('hello', arg2=5)
    """
    try:
        resp = httpx.get(f"{_some_api()}/{arg1}", timeout=30)
        resp.raise_for_status()
        return resp.text
    except httpx.HTTPStatusError as e:
        return _fmt_http_err(e, arg1)
```

Follow the tool naming convention: `domain_verb` (e.g. `memory_store`,
`job_create`). Return a string — never a dict.

---

## Adding a Hook Handler

Register in `pyclawops/hooks/registry.py` after the registry is created, or in
a bundled hook file. For a bundled hook:

1. Create `pyclawops/hooks/bundled/my-hook/handler.py`:

```python
from pyclawops.hooks.events import HookEvent

async def handle(payload: dict) -> None:
    """My hook handler."""
    # do something with payload
```

2. Register in `HookLoader.load_bundled()` for the appropriate event.

3. Add to the default `hooks.bundled` list in config examples.

---

## Adding a Config Section

1. Add a Pydantic model to `pyclawops/config/schema.py`:

```python
class MyFeatureConfig(BaseModel):
    """Configuration for my feature."""
    enabled: bool = True
    some_option: str = Field(
        default="default",
        validation_alias=AliasChoices("some_option", "someOption"),
    )
```

2. Add to the root `Config` model:

```python
class Config(BaseModel):
    ...
    my_feature: MyFeatureConfig = Field(
        default_factory=MyFeatureConfig,
        validation_alias=AliasChoices("my_feature", "myFeature"),
    )
```

3. Test with `MyFeatureConfig.model_validate({"someOption": "value"})`.

---

## Adding a REST API Route

1. Create `pyclawops/api/routes/my_thing.py`:

```python
from fastapi import APIRouter
router = APIRouter()

def _get_gateway():
    from pyclawops.api.app import get_gateway
    return get_gateway()

@router.get("/")
async def get_my_things():
    gateway = _get_gateway()
    return {"things": [...]}
```

2. Wire into `pyclawops/api/app.py`:

```python
from .routes import my_thing as my_thing_routes
app.include_router(my_thing_routes.router, prefix="/api/v1/my-thing", tags=["my-thing"])
```

---

## Adding a Provider

1. Create `pyclawops/providers/myprovider.py` implementing the provider interface.

2. Add a config class to `schema.py` and include in `ProvidersConfig`.

3. Register in `AgentFactory.build_fa_settings()` so FastAgent can use it.

4. Add model prefix detection in `AgentRunner` (e.g. `"myprovider/"` prefix
   on model names maps to FastAgent's provider config key).
