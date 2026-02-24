"""pyclaw - Python Gateway

A Python-based gateway inspired by OpenClaw, designed to be better,
cleaner, and more secure with its own architecture and design philosophy.
"""

__version__ = "0.1.0"
__author__ = "pyclaw team"

from .config import load_config, Config, ConfigLoader

__all__ = [
    "__version__",
    "load_config",
    "Config",
    "ConfigLoader",
]
