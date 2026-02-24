"""Main Gateway class for pyclaw."""

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from pyclaw.config.loader import ConfigLoader, Config
from pyclaw.config.schema import AgentConfig, SecurityConfig
from pyclaw.security.audit import AuditLogger
from pyclaw.security.approvals import ExecApprovalSystem
from pyclaw.security.sandbox import Sandbox, create_sandbox
from pyclaw.jobs.scheduler import JobScheduler
from pyclaw.core.agent import Agent, AgentManager
from pyclaw.core.session import SessionManager
from pyclaw.core.router import MessageRouter, IncomingMessage, OutgoingMessage


class Gateway:
    """Main Gateway class that orchestrates all pyclaw subsystems."""
    
    def __init__(self, config_path: Optional[str] = None):
        # Configuration
        self._config_loader = ConfigLoader(config_path)
        self._config: Optional[Config] = None
        
        # Subsystems
        self._audit_logger: Optional[AuditLogger] = None
        self._approval_system: Optional[ExecApprovalSystem] = None
        self._sandbox: Optional[Sandbox] = None
        self._job_scheduler: Optional[JobScheduler] = None
        
        # Core
        self._agent_manager: Optional[AgentManager] = None
        self._session_manager: Optional[SessionManager] = None
        self._router: Optional[MessageRouter] = None
        
        # Runtime
        self._is_running = False
        self._startup_tasks: List[asyncio.Task] = []
        self._logger = logging.getLogger("pyclaw.gateway")
        
        # Channel adapters (to be implemented)
        self._channels: Dict[str, Any] = {}
    
    @property
    def config(self) -> Config:
        """Get configuration."""
        if self._config is None:
            self._config = self._config_loader.load()
        return self._config
    
    @property
    def agent_manager(self) -> AgentManager:
        """Get agent manager."""
        if self._agent_manager is None:
            self._agent_manager = AgentManager()
        return self._agent_manager
    
    @property
    def session_manager(self) -> SessionManager:
        """Get session manager."""
        if self._session_manager is None:
            self._session_manager = SessionManager()
        return self._session_manager
    
    @property
    def router(self) -> MessageRouter:
        """Get message router."""
        if self._router is None:
            self._router = MessageRouter(self.config)
        return self._router
    
    @property
    def audit_logger(self) -> Optional[AuditLogger]:
        """Get audit logger."""
        return self._audit_logger
    
    @property
    def approval_system(self) -> Optional[ExecApprovalSystem]:
        """Get approval system."""
        return self._approval_system
    
    @property
    def sandbox(self) -> Optional[Sandbox]:
        """Get sandbox."""
        return self._sandbox
    
    @property
    def job_scheduler(self) -> Optional[JobScheduler]:
        """Get job scheduler."""
        return self._job_scheduler
    
    async def initialize(self) -> None:
        """Initialize all subsystems."""
        self._logger.info("Initializing pyclaw Gateway...")
        
        # Load config
        self._config = self._config_loader.load()
        self._logger.info(f"Loaded config (version: {self.config.version})")
        
        # Initialize security
        await self._init_security()
        
        # Initialize core
        await self._init_core()
        
        # Initialize channels
        await self._init_channels()
        
        # Initialize jobs
        await self._init_jobs()
        
        self._logger.info("Gateway initialization complete")
    
    async def _init_security(self) -> None:
        """Initialize security subsystem."""
        security_config: SecurityConfig = self.config.security
        
        # Audit logger
        if security_config.audit.enabled:
            self._audit_logger = AuditLogger(
                log_file=security_config.audit.log_file,
                retention_days=security_config.audit.retention_days,
            )
            self._logger.info("Audit logger initialized")
        
        # Exec approvals
        self._approval_system = ExecApprovalSystem(
            security_config.exec_approvals,
        )
        self._logger.info(
            f"Approval system initialized (mode: {security_config.exec_approvals.mode.value})"
        )
        
        # Sandbox
        self._sandbox = create_sandbox(security_config.sandbox)
        self._logger.info(
            f"Sandbox initialized (type: {security_config.sandbox.type})"
        )
    
    async def _init_core(self) -> None:
        """Initialize core subsystems."""
        # Session manager
        await self.session_manager.start()
        self._logger.info("Session manager started")
        
        # Create default agent from config
        for agent_id, agent_config_dict in self.config.agents.model_dump().items():
            name = agent_config_dict.get("name", agent_id)
            # Extract provider config if present
            provider_config = agent_config_dict.get("provider")
            # Convert dict to AgentConfig object
            agent_config = AgentConfig(**agent_config_dict)
            self.agent_manager.create_agent(
                agent_id=agent_id,
                name=name,
                config=agent_config,
                provider_config=provider_config,
                session_manager=self.session_manager,
            )
        
        await self.agent_manager.start_all()
        self._logger.info(f"Started {len(self.agent_manager.agents)} agents")
    
    async def _init_channels(self) -> None:
        """Initialize channel adapters."""
        # TODO: Implement channel adapters
        self._logger.info("Channel adapters not yet implemented")
    
    async def _init_jobs(self) -> None:
        """Initialize job scheduler."""
        self._job_scheduler = JobScheduler(self.config.jobs)
        await self._job_scheduler.start()
        self._logger.info("Job scheduler started")
    
    async def start(self) -> None:
        """Start the gateway."""
        if self._is_running:
            self._logger.warning("Gateway already running")
            return
        
        await self.initialize()
        
        self._is_running = True
        self._logger.info("pyclaw Gateway started")
        
        # Keep running
        try:
            while self._is_running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            self._logger.info("Gateway cancelled")
    
    async def stop(self) -> None:
        """Stop the gateway."""
        self._logger.info("Stopping pyclaw Gateway...")
        
        self._is_running = False
        
        # Stop agents
        if self._agent_manager:
            await self.agent_manager.stop_all()
        
        # Stop session manager
        if self._session_manager:
            await self.session_manager.stop()
        
        # Stop job scheduler
        if self._job_scheduler:
            await self.job_scheduler.stop()
        
        # Stop channels
        for channel in self._channels.values():
            if hasattr(channel, "stop"):
                await channel.stop()
        
        self._logger.info("pyclaw Gateway stopped")
    
    async def handle_message(
        self,
        channel: str,
        sender: str,
        sender_id: str,
        content: str,
        message_id: Optional[str] = None,
    ) -> Optional[str]:
        """Handle an incoming message."""
        # Create incoming message
        message = IncomingMessage(
            id=message_id or "",
            channel=channel,
            sender=sender,
            sender_id=sender_id,
            content=content,
        )
        
        # Get or create session
        session = await self.session_manager.get_or_create_session(
            agent_id="default",
            channel=channel,
            user_id=sender_id,
        )
        
        if session is None:
            return "Could not create session"
        
        # Log message
        if self._audit_logger:
            await self._audit_logger.log_message_received(
                session_id=session.id,
                agent_id=session.agent_id,
                channel=channel,
                user_id=sender_id,
                message_preview=content[:100],
            )
        
        # Get agent
        agent = self.agent_manager.get_agent(session.agent_id)
        if agent is None:
            return "No agent available"
        
        # Handle message
        response = await agent.handle_message(message, session)
        
        if response and self._audit_logger:
            await self._audit_logger.log(
                event_type="message_sent",
                agent_id=agent.id,
                session_id=session.id,
                channel=channel,
                user_id=sender_id,
            )
        
        return response.content if response else None
    
    async def run_heartbeats(self) -> None:
        """Run heartbeat checks for all agents."""
        for agent in self.agent_manager.list_agents():
            if agent.config.heartbeat.enabled:
                try:
                    result = await agent.run_heartbeat(
                        agent.config.heartbeat.prompt
                    )
                    if result:
                        self._logger.debug(f"Heartbeat result for {agent.name}: {result}")
                except Exception as e:
                    self._logger.error(f"Heartbeat error for {agent.name}: {e}")
    
    def get_status(self) -> Dict[str, Any]:
        """Get gateway status."""
        return {
            "is_running": self._is_running,
            "config_version": self.config.version,
            "security": {
                "audit_enabled": self._audit_logger is not None,
                "approval_mode": (
                    self._approval_system.mode.value
                    if self._approval_system else None
                ),
                "sandbox_type": (
                    self._config.security.sandbox.type
                    if self._config else None
                ),
            },
            "agents": self.agent_manager.get_status() if self._agent_manager else {},
            "sessions": self.session_manager.get_status() if self._session_manager else {},
            "jobs": self.job_scheduler.get_status() if self._job_scheduler else {},
        }


async def create_gateway(config_path: Optional[str] = None) -> Gateway:
    """Create and initialize a gateway."""
    gateway = Gateway(config_path)
    await gateway.initialize()
    return gateway
