"""Agent management for pyclaw with FastAgent integration."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pyclaw.utils.time import now
from typing import Any, Awaitable, Callable, Dict, List, Optional

from pyclaw.config.schema import AgentConfig as ConfigModel
from pyclaw.config.schema import AgentConfig
from pyclaw.core.session import Session
from pyclaw.core.router import IncomingMessage, OutgoingMessage
from pyclaw.providers import create_provider

# FastAgent imports
try:
    from fast_agent import FastAgent

    FASTAGENT_AVAILABLE = True
except Exception as _fa_import_err:
    import logging as _logging
    _logging.getLogger(__name__).warning(f"FastAgent not available: {type(_fa_import_err).__name__}: {_fa_import_err}")
    FastAgent = None
    FASTAGENT_AVAILABLE = False

from pyclaw.agents.factory import (
    FastAgentFactory,
    create_agent_from_config,
    get_factory,
)
from pyclaw.core.prompt_builder import build_system_prompt, AGENT_FILES


# Tool execution function type
ToolExecutor = Callable[[str, List[str], str], Awaitable[Dict[str, Any]]]


@dataclass
class Agent:
    """Agent that handles conversations."""

    id: str
    name: str
    config: ConfigModel
    session_manager: Any = None  # SessionManager
    tool_executor: Optional[ToolExecutor] = None
    provider: Optional[Any] = None  # Provider instance
    skill_runner: Optional[Any] = None  # SkillRunner for tool execution
    config_dir: str = "~/.pyclaw"  # Base config directory for agent files

    # FastAgent integration
    fast_agent: Optional[Any] = None  # FastAgent instance
    fast_agent_runner: Optional[Any] = None  # AgentRunner instance
    _session_runners: Dict[str, Any] = field(default_factory=dict)

    # Runtime state
    is_running: bool = False
    current_session: Optional[Session] = None
    _tasks: List[asyncio.Task] = field(default_factory=list)
    _logger: logging.Logger = field(init=False)

    def __post_init__(self):
        object.__setattr__(self, "_logger", logging.getLogger(f"pyclaw.agent.{self.id}"))
        object.__setattr__(self, "_session_runners", {})

        # Initialize FastAgent if available and configured
        if FASTAGENT_AVAILABLE and self._should_use_fastagent():
            self._init_fastagent()

    def _should_use_fastagent(self) -> bool:
        """Check if agent should use FastAgent."""
        # Use FastAgent if explicitly configured via use_fastagent flag
        if getattr(self.config, "use_fastagent", False):
            return True

        # Also check for workflow or model prefixes
        model = self.config.model.lower()
        return (
            model.startswith("fastagent")
            or model.startswith("fa:")
            or getattr(self.config, "workflow", None) is not None
        )

    @property
    def system_prompt(self) -> str:
        """Get system prompt - built from agent files or config."""
        # Try to build from agent files (like OpenClaw)
        if hasattr(self, "config_dir"):
            prompt = build_system_prompt(self.id, self.config_dir)
            if prompt != "You are a helpful AI assistant.":
                return prompt

        # Fall back to config
        return getattr(self.config, "system_prompt", "You are a helpful AI assistant.")

    def _init_fastagent(self) -> None:
        """Initialize FastAgent for this agent."""
        if not FASTAGENT_AVAILABLE:
            self._logger.warning("FastAgent not available")
            return

        try:
            import os
            factory = get_factory()

            # Translate "provider/model" → "<fastagent_provider>.<model>" using the
            # fastagent_provider field on the wired provider.  No provider names are
            # hardcoded here — the mapping lives entirely in the config file.
            raw_model = self.config.model
            for prefix in ["fastagent:", "fa:", "fastagent/"]:
                raw_model = raw_model.replace(prefix, "")

            fa_model = raw_model
            provider_api_key = None
            provider_base_url = None

            if hasattr(self, "provider") and self.provider:
                provider_api_key = getattr(self.provider, "api_key", None)
                # api_url is the OpenAI-compat base for FastAgent; base_url is the native endpoint
                provider_base_url = getattr(self.provider, "api_url", None)

            if "/" in raw_model:
                _, model_name = raw_model.split("/", 1)
                fa_prov = getattr(self.provider, "fastagent_provider", None) if self.provider else None
                if fa_prov:
                    prefix_upper = fa_prov.upper()
                    if provider_api_key:
                        os.environ[f"{prefix_upper}_API_KEY"] = provider_api_key
                    if provider_base_url:
                        os.environ[f"{prefix_upper}_BASE_URL"] = provider_base_url
                    fa_model = f"{fa_prov}.{model_name}"

            # Get workflow type from config
            workflow_type = getattr(self.config, "workflow", None)

            if workflow_type:
                workflow_config = {
                    "name": self.name,
                    "instruction": self.system_prompt,
                    "workflow": workflow_type,
                    "agents": getattr(self.config, "agents", []),
                    "model": fa_model,
                    "temperature": self.config.temperature,
                    "servers": getattr(self.config, "mcp_servers", []),
                }
                self.fast_agent = create_agent_from_config(workflow_config)
            else:
                self.fast_agent = factory.create_agent(
                    name=self.name,
                    instruction=self.system_prompt,
                    model=fa_model or "sonnet",
                    temperature=self.config.temperature,
                    max_tokens=self.config.max_tokens,
                    servers=getattr(self.config, "mcp_servers", []),
                )

            # Create runner for turn-based execution
            from pyclaw.agents.runner import AgentRunner

            mcp_servers = getattr(self.config, "mcp_servers", None) or []
            tools_cfg = {}
            if hasattr(self.config, "tools") and self.config.tools:
                t = self.config.tools
                tools_cfg = {
                    "profile": getattr(t, "profile", None),
                    "allow": getattr(t, "allow", []) or getattr(t, "allowlist", []),
                    "deny": getattr(t, "deny", []),
                }

            self.fast_agent_runner = AgentRunner(
                agent_name=self.name,
                instruction=self.system_prompt,
                model=fa_model or "sonnet",
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
                top_p=getattr(self.config, "top_p", None),
                max_iterations=getattr(self.config, "max_iterations", None),
                parallel_tool_calls=getattr(self.config, "parallel_tool_calls", None),
                streaming_timeout=getattr(self.config, "streaming_timeout", None),
                servers=mcp_servers,
                tools_config=tools_cfg,
                show_thinking=getattr(self.config, "show_thinking", False),
                api_key=provider_api_key,
                base_url=provider_base_url,
                request_params=getattr(self.config, "request_params", None),
            )

            self._logger.info(
                f"Initialized FastAgent for {self.name} "
                f"(model={fa_model}, servers={mcp_servers})"
            )

        except Exception as e:
            self._logger.error(f"Failed to initialize FastAgent: {e}", exc_info=True)

    def _get_session_runner(
        self,
        session_id: str,
        model_override: Optional[str] = None,
        history_path: Optional[Any] = None,
    ) -> Any:
        """Get or create a dedicated AgentRunner for a session.

        Each session gets its own runner so conversation histories are properly
        isolated across users.  Pass *model_override* to use a different model
        than the agent default for this session.  Pass *history_path* (a Path)
        to have the runner automatically load/save FA native history.
        """
        if not self.fast_agent_runner:
            raise RuntimeError(
                f"Agent {self.name} has no FastAgent runner configured"
            )
        if session_id not in self._session_runners:
            from pyclaw.agents.runner import AgentRunner
            base = self.fast_agent_runner
            effective_model = model_override or base.model
            runner = AgentRunner(
                agent_name=f"{self.name}-{session_id[:8]}",
                instruction=self.system_prompt,
                model=effective_model,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
                top_p=getattr(base, "top_p", None),
                max_iterations=getattr(base, "max_iterations", None),
                parallel_tool_calls=getattr(base, "parallel_tool_calls", None),
                streaming_timeout=getattr(base, "streaming_timeout", None),
                servers=base.servers,
                tools_config=base.tools_config,
                show_thinking=getattr(base, "show_thinking", False),
                api_key=getattr(base, "api_key", None),
                base_url=getattr(base, "base_url", None),
                owner_name=self.name,
                request_params=getattr(base, "request_params", None),
                history_path=history_path,
            )
            self._session_runners[session_id] = runner
            self._logger.debug(
                f"Created session runner for session {session_id[:8]} "
                f"(model={effective_model})"
            )
        return self._session_runners[session_id]

    async def evict_session_runner(self, session_id: str) -> None:
        """Tear down and remove the runner for a session so the next call gets a fresh one."""
        runner = self._session_runners.pop(session_id, None)
        if runner is not None:
            try:
                await runner.cleanup()
            except Exception:
                pass
            self._logger.debug(f"Evicted session runner for {session_id[:8]}")

    async def start(self) -> None:
        """Start the agent."""
        self.is_running = True
        self._logger.info(f"Agent {self.name} started")

        # Initialize FastAgent runner if available
        if self.fast_agent_runner:
            await self.fast_agent_runner.initialize()

    async def stop(self) -> None:
        """Stop the agent."""
        self.is_running = False

        # Cancel pending tasks
        for task in self._tasks:
            if not task.done():
                task.cancel()
        self._tasks.clear()

        # Close all session runners (releases FastAgent MCP connections)
        for runner in list(self._session_runners.values()):
            try:
                await runner.cleanup()
            except Exception:
                pass
        self._session_runners.clear()

        # Close the base runner
        if self.fast_agent_runner:
            try:
                await self.fast_agent_runner.cleanup()
            except Exception:
                pass

        self._logger.info(f"Agent {self.name} stopped")

    async def handle_message(
        self,
        message: IncomingMessage,
        session: Session,
    ) -> Optional[OutgoingMessage]:
        """Handle an incoming message."""
        self.current_session = session

        try:
            # Use FastAgent (only supported path)
            if not self.fast_agent_runner:
                raise RuntimeError(
                    f"Agent {self.name} has no FastAgent runner configured. FastAgent is required."
                )
            response_content = await self._handle_with_fastagent(message.content, session)
            session.touch(count_delta=2)

            return OutgoingMessage(
                content=response_content,
                target=message.sender_id,
                channel=message.channel,
                reply_to=message.id,
            )

        except Exception as e:
            self._logger.error(f"Error handling message: {e}")
            return OutgoingMessage(
                content=f"I encountered an error: {str(e)}",
                target=message.sender_id,
                channel=message.channel,
            )

    async def _handle_with_fastagent(
        self,
        prompt: str,
        session: Session,
    ) -> str:
        """Handle message using a per-session FastAgent runner."""
        model_override = session.context.get("model_override")
        runner = self._get_session_runner(
            session.id,
            model_override=model_override,
            history_path=session.history_path,
        )
        return await runner.run(prompt)

    async def execute_tool(
        self,
        tool_name: str,
        args: List[str],
        cwd: str,
    ) -> Dict[str, Any]:
        """Execute a tool."""
        if self.skill_runner:
            # Use skill runner
            result = await self.skill_runner.execute(
                skill_name=tool_name,
                args={"args": args, "cwd": cwd},
            )
            return {
                "success": result.success,
                "result": result.result,
                "error": result.error,
            }

        if self.tool_executor is None:
            return {
                "success": False,
                "error": "No tool executor configured",
            }

        return await self.tool_executor(tool_name, args, cwd)

    async def run_heartbeat(
        self,
        prompt: str,
    ) -> Optional[str]:
        """Run a heartbeat check."""
        if not self.config.heartbeat.enabled:
            return None

        # Check active hours
        active_hours = self.config.heartbeat.active_hours
        if active_hours:
            now = now()
            start = datetime.strptime(active_hours.get("start", "00:00"), "%H:%M")
            end = datetime.strptime(active_hours.get("end", "23:59"), "%H:%M")

            if not (start.time() <= now.time() <= end.time()):
                return None

        # Run heartbeat
        self._logger.debug("Running heartbeat")

        # Use FastAgent (only supported path)
        if not self.fast_agent_runner:
            self._logger.error(f"Agent {self.name} has no FastAgent runner for heartbeat")
            return None

        try:
            return await self.fast_agent_runner.run(prompt)
        except Exception as e:
            self._logger.error(f"FastAgent heartbeat error: {e}")
            return None

    def get_status(self) -> Dict[str, Any]:
        """Get agent status."""
        return {
            "id": self.id,
            "name": self.name,
            "is_running": self.is_running,
            "model": self.config.model,
            "session_id": self.current_session.id if self.current_session else None,
            "pending_tasks": len(self._tasks),
            "provider": type(self.provider).__name__ if self.provider else None,
            "fast_agent": self.fast_agent is not None,
            "skills": len(self.skill_runner.registry.list_skills()) if self.skill_runner else 0,
        }

    def update_config(self, **updates) -> None:
        """Update agent configuration."""
        for key, value in updates.items():
            if hasattr(self.config, key):
                setattr(self.config, key, value)
        self._logger.info(f"Updated config: {updates}")


class AgentManager:
    """Manages multiple agents."""

    def __init__(self):
        self.agents: Dict[str, Agent] = {}
        self._default_agent_id: Optional[str] = None
        self._logger = logging.getLogger("pyclaw.agent_manager")

    def create_agent(
        self,
        agent_id: str,
        name: str,
        config: ConfigModel,
        provider_config: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> Agent:
        """Create a new agent with optional provider."""
        if agent_id in self.agents:
            raise ValueError(f"Agent {agent_id} already exists")

        # Create provider if config provided
        provider = None
        if provider_config:
            self._logger.debug(f"provider_config = {provider_config}")
            provider_type = provider_config.get("type", "openai")
            provider = create_provider(provider_type, provider_config)

        agent = Agent(
            id=agent_id,
            name=name,
            config=config,
            provider=provider,
            **kwargs,
        )

        self.agents[agent_id] = agent

        if self._default_agent_id is None:
            self._default_agent_id = agent_id

        self._logger.info(f"Created agent: {name} ({agent_id})")

        return agent

    async def start_agent(self, agent_id: str) -> bool:
        """Start an agent."""
        agent = self.agents.get(agent_id)
        if agent:
            await agent.start()
            return True
        return False

    async def stop_agent(self, agent_id: str) -> bool:
        """Stop an agent."""
        agent = self.agents.get(agent_id)
        if agent:
            await agent.stop()
            return True
        return False

    async def start_all(self) -> None:
        """Start all agents."""
        for agent in self.agents.values():
            await agent.start()

    async def stop_all(self) -> None:
        """Stop all agents."""
        for agent in self.agents.values():
            await agent.stop()

    def get_agent(self, agent_id: str) -> Optional[Agent]:
        """Get an agent by ID."""
        return self.agents.get(agent_id)

    def get_default_agent(self) -> Optional[Agent]:
        """Get the default agent."""
        if self._default_agent_id:
            return self.agents.get(self._default_agent_id)
        return None

    def set_default_agent(self, agent_id: str) -> bool:
        """Set the default agent."""
        if agent_id in self.agents:
            self._default_agent_id = agent_id
            return True
        return False

    def remove_agent(self, agent_id: str) -> bool:
        """Remove an agent."""
        agent = self.agents.pop(agent_id, None)
        if agent:
            if self._default_agent_id == agent_id:
                self._default_agent_id = list(self.agents.keys())[0] if self.agents else None
            self._logger.info(f"Removed agent: {agent_id}")
            return True
        return False

    def list_agents(self) -> List[Agent]:
        """List all agents."""
        return list(self.agents.values())

    def get_status(self) -> Dict[str, Any]:
        """Get agent manager status."""
        return {
            "total_agents": len(self.agents),
            "running_agents": len([a for a in self.agents.values() if a.is_running]),
            "default_agent": self._default_agent_id,
            "agents": [
                {
                    "id": a.id,
                    "name": a.name,
                    "is_running": a.is_running,
                    "model": a.config.model,
                }
                for a in self.agents.values()
            ],
        }

