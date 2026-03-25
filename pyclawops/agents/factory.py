"""FastAgent factory for creating agents from YAML configuration.

This module provides a factory that compiles YAML agent definitions
into FastAgent decorator-based code. The key insight is:

    YAML: agents: {order_agent, ship_agent, order_ship: chain}
    ↓
    FastAgent: @fast.agent(order_agent), @fast.agent(ship_agent), 
                @fast.chain(sequence=[order_agent, ship_agent])
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
import importlib.util
import sys

import yaml

try:
    from fast_agent import FastAgent
    FASTAGENT_AVAILABLE = True
except ImportError:
    FastAgent = None
    FASTAGENT_AVAILABLE = False


logger = logging.getLogger("pyclawops.agents.factory")


class FastAgentCompilationError(Exception):
    """Raised when YAML agent config cannot be compiled to FastAgent code.

    Attributes:
        args: Inherited exception arguments describing the compilation failure.
    """
    pass


class FastAgentFactory:
    """Factory for compiling YAML config to FastAgent.
    
    This factory reads YAML configuration and can either:
    1. Generate Python code with @fast decorators
    2. Dynamically create and run agents
    
    YAML Format:
    ```yaml
    agents:
      order_agent:
        instruction: "Process orders..."
        model: sonnet
        servers: [fetch]
        
      ship_agent:
        instruction: "Ship orders..."
        
      order_ship:
        chain: [order_agent, ship_agent]
        
    workflows:
      parallel_review:
        parallel:
          fan_out: [proofreader, fact_checker]
          fan_in: grader
    ```
    """
    
    def __init__(self, config_path: str = "agents.yaml"):
        """Initialize the factory.
        
        Args:
            config_path: Path to YAML config file.
        """
        self.config_path = config_path
        self._config: Dict[str, Any] = {}
        self._fast_instance: Optional["FastAgent"] = None
        self._compiled_agents: Dict[str, Any] = {}
    
    @property
    def is_available(self) -> bool:
        """Check whether the fast-agent-mcp package is installed and importable.

        Returns:
            bool: True if FastAgent is available, False otherwise.
        """
        return FASTAGENT_AVAILABLE
    
    def _ensure_fastagent(self) -> None:
        """Ensure FastAgent is available, raising ImportError if not installed.

        Returns:
            None

        Raises:
            ImportError: If fast-agent-mcp is not installed in the environment.
        """
        if not FASTAGENT_AVAILABLE:
            raise ImportError(
                "FastAgent is not installed. "
                "Install with: uv pip install fast-agent-mcp"
            )
    
    def load_config(self, path: Optional[str] = None) -> Dict[str, Any]:
        """Load and parse the YAML agent configuration file.

        Tries the provided path first; if not found, tries a ``.yaml`` variant:
        if the path ends with ``.yml`` it replaces that suffix with ``.yaml``,
        otherwise it appends ``.yaml`` to the path.

        Args:
            path (Optional[str]): Path to the YAML config file. Defaults to
                ``self.config_path``.

        Returns:
            Dict[str, Any]: Parsed YAML configuration dictionary.

        Raises:
            FileNotFoundError: If neither the given path nor the ``.yaml`` variant exists.
        """
        config_path = path or self.config_path
        
        if not os.path.exists(config_path):
            # Try with .yaml extension
            yaml_path = config_path.replace('.yml', '.yaml') if config_path.endswith('.yml') else config_path + '.yaml'
            if os.path.exists(yaml_path):
                config_path = yaml_path
            else:
                raise FileNotFoundError(f"Config file not found: {config_path}")
        
        with open(config_path, 'r') as f:
            self._config = yaml.safe_load(f) or {}
        
        logger.info(f"Loaded config from {config_path}")
        return self._config
    
    def compile_to_code(self, config: Optional[Dict[str, Any]] = None) -> str:
        """Compile YAML config to Python code with FastAgent decorators.

        Generates the actual Python code that would be written if you were
        manually creating FastAgent agents.  Emits ``@fast.agent``,
        ``@fast.chain``, ``@fast.parallel``, and ``@fast.router`` decorators
        derived from the YAML structure, plus a boilerplate ``main()`` function.

        Args:
            config (Optional[Dict[str, Any]]): Config dict to compile. If None,
                uses ``self._config`` or loads from ``self.config_path``.

        Returns:
            str: Generated Python source code as a string.
        """
        config = config or self._config
        if not config:
            config = self.load_config()
        
        agents = config.get('agents', {})
        workflows = config.get('workflows', {})
        
        lines = [
            '"""Auto-generated FastAgent code from pyclawops config."""',
            '',
            'import asyncio',
            'from fast_agent import FastAgent',
            '',
            '',
            '# Create the FastAgent application',
            'fast = FastAgent("pyclawops agents")',
            '',
        ]
        
        # Track chain/parallel definitions to apply decorators in order
        chain_defs = []
        parallel_defs = []
        router_defs = []
        
        # First pass: collect workflow definitions
        for name, spec in workflows.items():
            if isinstance(spec, dict):
                wf_type = spec.get('chain') or spec.get('parallel') or spec.get('router')
                if spec.get('chain'):
                    chain_defs.append((name, spec['chain']))
                elif spec.get('parallel'):
                    parallel_defs.append((name, spec['parallel']))
                elif spec.get('router'):
                    router_defs.append((name, spec['router']))
        
        # Second pass: emit agent decorators
        # Agents referenced in chains need to be defined first
        defined_agents = set()
        
        # Define agents that are part of chains
        for chain_name, sequence in chain_defs:
            for agent_name in sequence:
                if agent_name not in defined_agents and agent_name in agents:
                    lines.extend(self._emit_agent_decorator(agent_name, agents[agent_name]))
                    defined_agents.add(agent_name)
        
        # Define remaining agents
        for name, spec in agents.items():
            if name not in defined_agents:
                lines.extend(self._emit_agent_decorator(name, spec))
                defined_agents.add(name)
        
        # Emit chain decorators
        for chain_name, sequence in chain_defs:
            default = 'default=True' if chain_name == config.get('default_agent') else ''
            lines.append(
                f'@fast.chain(name="{chain_name}", sequence={sequence}, {default})'
            )
        
        # Emit parallel decorators
        for para_name, spec in parallel_defs:
            fan_out = spec.get('fan_out', [])
            fan_in = spec.get('fan_in', '')
            lines.append(
                f'@fast.parallel(name="{para_name}", fan_out={fan_out}, fan_in="{fan_in}")'
            )
        
        # Emit router decorators
        for router_name, spec in router_defs:
            agents_list = spec.get('agents', [])
            lines.append(
                f'@fast.router(name="{router_name}", agents={agents_list})'
            )
        
        # Main function
        lines.extend([
            '',
            'async def main():',
            '    async with fast.run() as agent:',
            '        # Use agent.<name>.send("message") to invoke',
            '        pass',
            '',
            '',
            'if __name__ == "__main__":',
            '    asyncio.run(main())',
        ])
        
        return '\n'.join(lines)
    
    def _emit_agent_decorator(self, name: str, spec: Dict[str, Any]) -> List[str]:
        """Emit the ``@fast.agent(...)`` decorator code line for a single agent.

        Args:
            name (str): Agent name used as the ``name=`` argument.
            spec (Dict[str, Any]): Agent specification dict with optional keys
                ``instruction``, ``model``, ``servers``, and ``human_input``.

        Returns:
            List[str]: A list containing the single decorator line string.
        """
        instruction = spec.get('instruction', f'You are {name}.')
        model = spec.get('model', 'sonnet')
        servers = spec.get('servers', [])
        human_input = spec.get('human_input', False)
        
        # Build keyword arguments
        kwargs = [f'name="{name}"']
        kwargs.append(f'instruction="""{instruction}"""')
        
        if model and model != 'sonnet':
            kwargs.append(f'model="{model}"')
        if servers:
            kwargs.append(f'servers={servers}')
        if human_input:
            kwargs.append('human_input=True')
        
        # Check if this is also referenced as a workflow child
        # (handled separately)
        
        return [f'@fast.agent({", ".join(kwargs)})']
    
    def create_from_config(
        self,
        config: Optional[Dict[str, Any]] = None,
        model_override: Optional[str] = None
    ) -> "FastAgent":
        """Create a FastAgent instance from config.

        Note: This creates the FastAgent app but decorators must be
        applied at module import time. For full support, use ``compile_to_code()``
        to generate Python code and import it.

        Args:
            config (Optional[Dict[str, Any]]): Config dict to use. If None, loads
                from ``self.config_path``.
            model_override (Optional[str]): Override model for all agents.
                Currently stored but not applied automatically. Defaults to None.

        Returns:
            FastAgent: A partially-configured FastAgent instance (decorators not yet applied).

        Raises:
            ImportError: If fast-agent-mcp is not installed.
        """
        self._ensure_fastagent()
        
        config = config or self._load_config()
        
        # Create base FastAgent
        self._fast_instance = FastAgent("pyclawops")
        
        # Store config for reference
        self._compiled_agents = config.get('agents', {})
        
        return self._fast_instance
    
    def generate_agent_module(
        self,
        output_path: str,
        config: Optional[Dict[str, Any]] = None
    ) -> None:
        """Generate a ready-to-run Python module file from config.

        Compiles the YAML config to Python source code and writes it to the
        given path as a standalone script with all FastAgent decorators applied.

        Args:
            output_path (str): File path to write the generated Python module.
            config (Optional[Dict[str, Any]]): Config dict to compile. If None,
                uses ``self._config`` or loads from ``self.config_path``.

        Returns:
            None
        """
        config = config or self._config
        if not config:
            config = self.load_config()
        
        code = self.compile_to_code(config)
        
        with open(output_path, 'w') as f:
            f.write(code)
        
        logger.info(f"Generated FastAgent module: {output_path}")
    
    def run_agent(
        self,
        agent_name: str,
        message: str,
        config: Optional[Dict[str, Any]] = None
    ) -> str:
        """Run a specific agent with a message and return the response.

        For full workflow support, generate code with ``compile_to_code()`` and
        run it directly.  This is a simplified interface for single agents.

        Args:
            agent_name (str): Name of the agent to run.
            message (str): Message to send to the agent.
            config (Optional[Dict[str, Any]]): Config dict to use. If None, uses
                ``self._config`` or loads from ``self.config_path``.

        Returns:
            str: The agent's response text.

        Raises:
            ValueError: If the agent name is not found in the config.
        """
        config = config or self._config
        if not config:
            config = self.load_config()
        
        agents = config.get('agents', {})
        
        if agent_name not in agents:
            raise ValueError(f"Agent not found: {agent_name}")
        
        spec = agents[agent_name]
        
        # Use the simpler API for single agent execution
        return asyncio.run(self._run_agent_async(agent_name, message, spec))
    
    async def _run_agent_async(
        self,
        agent_name: str,
        message: str,
        spec: Dict[str, Any]
    ) -> str:
        """Async helper for running a single agent with a given spec.

        Creates a FastAgent ``Context`` from the agent spec, enters it, sends the
        message, and returns the response as a string.

        Args:
            agent_name (str): Name of the agent.
            message (str): Message to send.
            spec (Dict[str, Any]): Agent specification with optional ``instruction``,
                ``model``, and ``servers`` keys.

        Returns:
            str: The agent's response text.
        """
        from fast_agent import Context
        
        instruction = spec.get('instruction', f'You are {agent_name}.')
        model = spec.get('model', 'sonnet')
        servers = spec.get('servers', [])
        
        # Create agent using Context for simpler execution
        context = Context(
            name=agent_name,
            instruction=instruction,
            model=model,
            servers=servers,
        )
        
        async with context:
            result = await context.send(message)
            return str(result)
    
    def _load_config(self) -> Dict[str, Any]:
        """Return the cached config, loading from disk if not yet loaded.

        Alias for ``load_config()`` when config has not been explicitly loaded.

        Returns:
            Dict[str, Any]: Parsed YAML configuration dictionary.
        """
        if not self._config:
            self.load_config()
        return self._config
    
    # Legacy methods for backwards compatibility
    def create_agent(
        self,
        name: str,
        instruction: str,
        model: str = "sonnet",
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        servers: Optional[List[str]] = None,
        human_input: bool = False,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> "FastAgent":
        """Legacy method to store an agent config and return a FastAgent instance.

        Stores the agent specification in ``self._legacy_agents`` for reference.
        For full decorator support at runtime, use ``compile_to_code()`` instead.

        Args:
            name (str): Agent name.
            instruction (str): System prompt for the agent.
            model (str): Model string. Defaults to ``"sonnet"``.
            temperature (float): Sampling temperature. Defaults to 0.7.
            max_tokens (Optional[int]): Maximum tokens per response. Defaults to None.
            servers (Optional[List[str]]): MCP server names. Defaults to None.
            human_input (bool): Whether the agent can request human input. Defaults to False.
            tools (Optional[List[Dict[str, Any]]]): Tool configuration list. Defaults to None.

        Returns:
            FastAgent: The existing ``self._fast_instance`` or a new ``FastAgent("pyclawops")``
            instance.

        Raises:
            ImportError: If fast-agent-mcp is not installed.
        """
        self._ensure_fastagent()
        
        # Store agent config
        if not hasattr(self, '_legacy_agents'):
            self._legacy_agents = {}
        
        self._legacy_agents[name] = {
            'instruction': instruction,
            'model': model,
            'servers': servers or [],
            'human_input': human_input,
        }
        
        logger.info(f"Created legacy agent config: {name}")
        return self._fast_instance or FastAgent("pyclawops")
    
    def create_chain_workflow(
        self,
        name: str,
        sequence: List[str],
        instruction: Optional[str] = None,
    ) -> Any:
        """Legacy method to register a chain workflow by name.

        Logs the workflow definition and returns the name for downstream use.
        No actual FastAgent decorator is applied at runtime; use
        ``compile_to_code()`` to generate the decorator-based code.

        Args:
            name (str): Workflow name.
            sequence (List[str]): Ordered list of agent names in the chain.
            instruction (Optional[str]): Optional orchestrator instruction. Defaults to None.

        Returns:
            Any: The workflow name string.
        """
        logger.info(f"Chain workflow '{name}': {sequence}")
        return name
    
    def create_parallel_workflow(
        self,
        name: str,
        fan_out: List[str],
        fan_in: Optional[str] = None,
        instruction: Optional[str] = None,
    ) -> Any:
        """Legacy method to register a parallel workflow by name.

        Logs the workflow definition and returns the name for downstream use.

        Args:
            name (str): Workflow name.
            fan_out (List[str]): List of agent names that run in parallel.
            fan_in (Optional[str]): Aggregator agent name. Defaults to None.
            instruction (Optional[str]): Optional orchestrator instruction. Defaults to None.

        Returns:
            Any: The workflow name string.
        """
        logger.info(f"Parallel workflow '{name}': {fan_out} -> {fan_in}")
        return name
    
    def create_maker_workflow(
        self,
        name: str,
        worker: str,
        k: int = 3,
        max_samples: int = 25,
        match_strategy: str = "normalized",
        instruction: Optional[str] = None,
    ) -> Any:
        """Legacy method to register a maker (K-voting) workflow by name.

        Logs the workflow definition and returns the name for downstream use.

        Args:
            name (str): Workflow name.
            worker (str): Worker agent name that generates candidate responses.
            k (int): Number of candidates to generate. Defaults to 3.
            max_samples (int): Maximum total samples. Defaults to 25.
            match_strategy (str): Strategy for selecting the winner. Defaults to ``"normalized"``.
            instruction (Optional[str]): Optional orchestrator instruction. Defaults to None.

        Returns:
            Any: The workflow name string.
        """
        logger.info(f"Maker workflow '{name}': worker={worker}, k={k}")
        return name
    
    def create_agents_as_tools_workflow(
        self,
        name: str,
        agents: List[str],
        instruction: str,
        default: bool = True,
        servers: Optional[List[str]] = None,
    ) -> Any:
        """Legacy method to register an agents-as-tools (orchestrator) workflow by name.

        Logs the workflow definition and returns the name for downstream use.

        Args:
            name (str): Workflow name.
            agents (List[str]): List of sub-agent names available as tools.
            instruction (str): System prompt for the orchestrator agent.
            default (bool): Whether this is the default agent. Defaults to True.
            servers (Optional[List[str]]): MCP server names for the orchestrator. Defaults to None.

        Returns:
            Any: The workflow name string.
        """
        logger.info(f"Agents-as-tools workflow '{name}': {agents}")
        return name


# Global factory instance
_factory: Optional[FastAgentFactory] = None


def get_factory(config_path: str = "agents.yaml") -> FastAgentFactory:
    """Return the global FastAgentFactory singleton, creating it if necessary.

    Args:
        config_path (str): Path to the YAML config file used when creating the
            factory for the first time. Defaults to ``"agents.yaml"``.

    Returns:
        FastAgentFactory: The global factory instance.
    """
    global _factory
    if _factory is None:
        _factory = FastAgentFactory(config_path)
    return _factory


def compile_yaml_config(
    yaml_path: str,
    output_path: Optional[str] = None
) -> str:
    """Compile YAML config to FastAgent Python code.
    
    Args:
        yaml_path: Path to YAML config
        output_path: Optional path to write generated code
        
    Returns:
        Generated Python code
    """
    factory = FastAgentFactory(yaml_path)
    code = factory.compile_to_code()
    
    if output_path:
        with open(output_path, 'w') as f:
            f.write(code)
        logger.info(f"Wrote generated code to {output_path}")
    
    return code


def run_agent_from_config(
    yaml_path: str,
    agent_name: str,
    message: str
) -> str:
    """Run an agent directly from YAML config.
    
    Args:
        yaml_path: Path to YAML config
        agent_name: Name of agent to run
        message: Message to send
        
    Returns:
        Agent response
    """
    factory = FastAgentFactory(yaml_path)
    return factory.run_agent(agent_name, message)


def create_agent_from_config(config: Dict[str, Any]) -> Any:
    """Create a FastAgent or workflow from a configuration dictionary.

    Dispatches to the appropriate factory method based on the ``workflow`` key.
    Supported workflow types: ``chain``, ``parallel``, ``maker``,
    ``agents_as_tools``.  If no ``workflow`` key is present, creates a regular
    single agent.

    Args:
        config (Dict[str, Any]): Agent configuration dictionary with keys:
            - ``name`` (str): Agent name.
            - ``instruction`` (str): System prompt.
            - ``model`` (str): Model to use.
            - ``temperature`` (float): Sampling temperature.
            - ``max_tokens`` (Optional[int]): Max tokens per response.
            - ``servers`` (Optional[List[str]]): MCP server names.
            - ``workflow`` (Optional[str]): Workflow type, one of ``chain``,
              ``parallel``, ``maker``, or ``agents_as_tools``.
            - ``agents`` (Optional[List[str]]): Sub-agent names for workflow patterns.
            - ``fan_in`` (Optional[str]): Aggregator agent for parallel workflow.
            - ``worker`` (Optional[str]): Worker agent for maker workflow.
            - ``k`` (int): K-voting count for maker workflow.
            - ``max_samples`` (int): Max samples for maker workflow.
            - ``human_input`` (bool): Whether agent can request human input.
            - ``tools`` (Optional[list]): Tool configuration.

    Returns:
        Any: A FastAgent instance (regular agent) or workflow name string (legacy
        workflow types).
    """
    factory = get_factory()
    
    workflow_type = config.get("workflow")
    
    if workflow_type == "chain":
        return factory.create_chain_workflow(
            name=config.get("name", "chain"),
            sequence=config.get("agents", []),
            instruction=config.get("instruction"),
        )
    elif workflow_type == "parallel":
        return factory.create_parallel_workflow(
            name=config.get("name", "parallel"),
            fan_out=config.get("agents", []),
            fan_in=config.get("fan_in"),
            instruction=config.get("instruction"),
        )
    elif workflow_type == "maker":
        return factory.create_maker_workflow(
            name=config.get("name", "maker"),
            worker=config.get("worker", config.get("agents", [None])[0]),
            k=config.get("k", 3),
            max_samples=config.get("max_samples", 25),
            instruction=config.get("instruction"),
        )
    elif workflow_type == "agents_as_tools":
        return factory.create_agents_as_tools_workflow(
            name=config.get("name", "orchestrator"),
            agents=config.get("agents", []),
            instruction=config.get("instruction", "Use the available agents."),
            servers=config.get("servers"),
        )
    else:
        # Regular agent
        return factory.create_agent(
            name=config.get("name", "agent"),
            instruction=config.get("instruction", "You are a helpful assistant."),
            model=config.get("model", "sonnet"),
            temperature=config.get("temperature", 0.7),
            max_tokens=config.get("max_tokens"),
            servers=config.get("servers"),
            human_input=config.get("human_input", False),
            tools=config.get("tools"),
        )


# Legacy methods on factory for backwards compatibility
def create_chain_workflow(
    name: str,
    sequence: List[str],
    instruction: Optional[str] = None,
) -> Any:
    """Module-level legacy helper to create a chain workflow via the global factory.

    Args:
        name (str): Workflow name.
        sequence (List[str]): Ordered list of agent names in the chain.
        instruction (Optional[str]): Optional orchestrator instruction. Defaults to None.

    Returns:
        Any: The workflow name string returned by the factory.
    """
    return get_factory().create_chain_workflow(name, sequence, instruction)


def create_parallel_workflow(
    name: str,
    fan_out: List[str],
    fan_in: Optional[str] = None,
    instruction: Optional[str] = None,
) -> Any:
    """Module-level legacy helper to create a parallel workflow via the global factory.

    Args:
        name (str): Workflow name.
        fan_out (List[str]): List of agent names that run in parallel.
        fan_in (Optional[str]): Aggregator agent name. Defaults to None.
        instruction (Optional[str]): Optional orchestrator instruction. Defaults to None.

    Returns:
        Any: The workflow name string returned by the factory.
    """
    return get_factory().create_parallel_workflow(name, fan_out, fan_in, instruction)


__all__ = [
    "FastAgentFactory",
    "FastAgentCompilationError",
    "compile_yaml_config",
    "run_agent_from_config",
    "create_agent_from_config",
    "get_factory",
    "create_chain_workflow",
    "create_parallel_workflow",
    "FASTAGENT_AVAILABLE",
]
