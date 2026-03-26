"""Workflow patterns for pyclopse using FastAgent."""

from .chain import ChainWorkflow, run_chain
from .parallel import ParallelWorkflow, run_parallel
from .agents_as_tools import AgentsAsToolsWorkflow, run_agents_as_tools

__all__ = [
    "ChainWorkflow",
    "run_chain",
    "ParallelWorkflow", 
    "run_parallel",
    "AgentsAsToolsWorkflow",
    "run_agents_as_tools",
]
