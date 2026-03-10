"""Agent runner using FastAgent."""
import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

# Regex to strip <thinking>...</thinking> blocks (case-insensitive, dotall)
_THINKING_RE = re.compile(r"<thinking>.*?</thinking>", re.DOTALL | re.IGNORECASE)


def strip_thinking_tags(text: str) -> str:
    """Remove <thinking>...</thinking> blocks from *text* and normalise whitespace."""
    stripped = _THINKING_RE.sub("", text)
    # Collapse more than two consecutive newlines left behind by removals
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    return stripped.strip()

logger = logging.getLogger(__name__)

# Default MCP servers every agent gets (can be extended per-agent)
_DEFAULT_SERVERS: List[str] = []

# All available server names (defined in fastagent.config.yaml)
ALL_SERVERS = ["pyclaw", "fetch", "time", "filesystem"]


class AgentRunner:
    """
    Runner for FastAgent-based execution.

    Wires MCP servers from agent config so tools are available to the agent.
    """

    def __init__(
        self,
        agent_name: str,
        instruction: str,
        model: str = "sonnet",
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        servers: Optional[List[str]] = None,
        tools_config: Optional[Dict[str, Any]] = None,
        show_thinking: bool = False,
        api_key: Optional[str] = None,
        owner_name: Optional[str] = None,
    ):
        self.agent_name = agent_name
        self.instruction = instruction
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        # servers = list of MCP server names from fastagent.config.yaml
        self.servers: List[str] = servers or list(_DEFAULT_SERVERS)
        self.tools_config = tools_config or {}
        # When False, <thinking>…</thinking> blocks are stripped before returning
        self.show_thinking = show_thinking
        # Optional API key (e.g. from pyclaw.yaml providers.minimax.api_key)
        self.api_key = api_key
        # The agent name sent as X-Agent-Name header to the pyclaw MCP server.
        # Defaults to agent_name but session runners override to the base agent name.
        self.owner_name: str = owner_name or agent_name
        self._app: Optional[Any] = None
        self._message_history: List[Dict[str, str]] = []

    async def initialize(self):
        """Initialize the FastAgent app with configured MCP servers."""
        if self._app is not None:
            return

        # Set up MiniMax generic provider if needed
        if "generic." in self.model:
            # Priority: explicit api_key arg → MINIMAX_API_KEY env → keychain
            api_key = self.api_key or os.environ.get("MINIMAX_API_KEY")
            if not api_key:
                try:
                    import subprocess
                    api_key = subprocess.check_output(
                        ["security", "find-generic-password", "-s", "pyclaw",
                         "-a", "minimax-api-key", "-w"],
                        text=True,
                    ).strip()
                except Exception:
                    pass
            if api_key:
                os.environ["GENERIC_API_KEY"] = api_key
            os.environ["GENERIC_BASE_URL"] = "https://api.minimax.io/v1"

        # Ensure fastagent.config.yaml is findable from CWD
        _ensure_fastagent_config()

        from fast_agent import FastAgent

        fast = FastAgent(self.agent_name)

        # Inject X-Agent-Name header into the pyclaw HTTP MCP server config
        # so the server can identify which agent is calling.
        try:
            pyclaw_cfg = fast.config.get("mcp", {}).get("servers", {}).get("pyclaw")
            if pyclaw_cfg is not None and isinstance(pyclaw_cfg, dict):
                existing = pyclaw_cfg.get("headers") or {}
                pyclaw_cfg["headers"] = {**existing, "x-agent-name": self.owner_name}
        except Exception as e:
            logger.debug(f"Could not set X-Agent-Name header on pyclaw MCP server: {e}")

        @fast.agent(
            instruction=self.instruction,
            model=self.model,
            servers=self.servers,
        )
        async def main():
            pass

        async with fast.run() as app:
            self._app = app
            logger.info(
                f"Initialized agent runner: {self.agent_name} "
                f"(model={self.model}, servers={self.servers})"
            )
    
    async def run(self, prompt: str) -> str:
        """Run a single prompt through the agent.
        
        Args:
            prompt: User prompt
            
        Returns:
            Agent response content
        """
        if self._app is None:
            await self.initialize()
        
        # Add to history
        self._message_history.append({"role": "user", "content": prompt})

        # Enforce per-model concurrency limit
        from pyclaw.core.concurrency import get_manager
        async with get_manager().acquire(self.model):
            result = await self._app.send(prompt)
        response = str(result)

        # Strip <thinking> blocks unless explicitly shown
        if not self.show_thinking:
            response = strip_thinking_tags(response)
        
        # Add response to history
        self._message_history.append({"role": "assistant", "content": response})
        
        return response
    
    async def run_stream(self, prompt: str) -> AsyncIterator[str]:
        """Run a prompt and stream the response.
        
        Args:
            prompt: User prompt
            
        Yields:
            Response chunks in real-time
        """
        if self._app is None:
            await self.initialize()
        
        self._message_history.append({"role": "user", "content": prompt})
        
        # Get the agent and set up streaming
        agent = self._app._agent(None)
        
        # Check if agent supports streaming
        if hasattr(agent, 'add_stream_listener'):
            import asyncio
            from collections import deque
            
            # Use a queue to stream chunks as they arrive
            chunk_queue = deque()
            send_done = False
            
            def on_chunk(chunk):
                # Extract text from StreamChunk
                if hasattr(chunk, 'text'):
                    chunk_queue.append(chunk.text)
                else:
                    chunk_queue.append(str(chunk))
            
            remove_listener = agent.add_stream_listener(on_chunk)
            
            try:
                # Start send in background
                send_task = asyncio.create_task(agent.send(prompt))
                
                # Yield chunks as they arrive
                while not send_done or chunk_queue:
                    while chunk_queue:
                        yield chunk_queue.popleft()
                    if send_task.done():
                        send_done = True
                    else:
                        await asyncio.sleep(0.01)  # Small delay to allow chunks to arrive
                
                # Wait for send to complete
                await send_task
                
            finally:
                remove_listener()
        else:
            # Fall back to non-streaming
            result = await self._app.send(prompt)
            yield str(result)
    
    def get_history(self) -> List[Dict[str, str]]:
        """Get message history."""
        return self._message_history.copy()


def _ensure_fastagent_config() -> None:
    """
    Make sure a fastagent.config.yaml is findable from the current directory.
    FastAgent looks in CWD, then walks up. We also check ~/.pyclaw/.
    """
    cwd = Path.cwd()
    # Already present in cwd or a parent?
    for p in [cwd, *cwd.parents]:
        if (p / "fastagent.config.yaml").exists():
            return

    # Try ~/.pyclaw/fastagent.config.yaml — symlink into CWD if found
    user_cfg = Path("~/.pyclaw/fastagent.config.yaml").expanduser()
    if user_cfg.exists():
        target = cwd / "fastagent.config.yaml"
        try:
            target.symlink_to(user_cfg)
            logger.debug(f"Symlinked fastagent.config.yaml → {user_cfg}")
        except (FileExistsError, OSError):
            pass
        return

    # Fall back: check the project source tree
    src_cfg = Path(__file__).parent.parent.parent / "fastagent.config.yaml"
    if src_cfg.exists():
        target = cwd / "fastagent.config.yaml"
        try:
            target.symlink_to(src_cfg.resolve())
        except (FileExistsError, OSError):
            pass


