"""
Channel plugin discovery and loading.

Two discovery mechanisms are supported (both can be active at once):

1. **Entry points** — installed packages declare a ``pyclaw.channels`` entry
   point group.  Every entry point in that group is treated as a channel
   plugin class::

       [project.entry-points."pyclaw.channels"]
       discord = "mypackage.discord:DiscordPlugin"

2. **Explicit config** — the ``plugins.channels`` list in ``pyclaw.yaml``
   contains ``"module.path:ClassName"`` strings::

       plugins:
         channels:
           - mypackage.discord:DiscordPlugin
           - mypackage.whatsapp:WhatsAppPlugin

In both cases the referenced class must be a subclass of
:class:`~pyclaw.channels.plugin.ChannelPlugin`.
"""

import importlib
import logging
from typing import List, Type

from .plugin import ChannelPlugin

logger = logging.getLogger("pyclaw.channels")

_ENTRY_POINT_GROUP = "pyclaw.channels"


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------

def _load_class(spec: str) -> Type[ChannelPlugin]:
    """Import and return a ChannelPlugin class from a ``"module:Class"`` string.

    Args:
        spec (str): Dotted module path and class name separated by ``:``,
            e.g. ``"mypackage.discord:DiscordPlugin"``.

    Returns:
        Type[ChannelPlugin]: The imported plugin class.

    Raises:
        ValueError: If ``spec`` does not contain a ``:`` separator.
        ImportError: If the module cannot be imported or the class name is
            not found in the module.
        TypeError: If the resolved class is not a subclass of
            :class:`ChannelPlugin`.
    """
    if ":" not in spec:
        raise ValueError(
            f"Invalid channel plugin spec {spec!r}. "
            "Expected 'module.path:ClassName'."
        )
    module_path, class_name = spec.rsplit(":", 1)
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name, None)
    if cls is None:
        raise ImportError(
            f"Class '{class_name}' not found in module '{module_path}'"
        )
    if not (isinstance(cls, type) and issubclass(cls, ChannelPlugin)):
        raise TypeError(
            f"{spec!r} must be a subclass of ChannelPlugin, got {cls!r}"
        )
    return cls


def load_from_specs(specs: List[str]) -> List[ChannelPlugin]:
    """Instantiate channel plugins from a list of ``"module:Class"`` strings.

    Errors for individual specs are logged and skipped so that the gateway
    does not fail to start because a single plugin spec is wrong.

    Args:
        specs (List[str]): List of ``"module.path:ClassName"`` strings,
            typically from ``plugins.channels`` in the config file.

    Returns:
        List[ChannelPlugin]: Successfully loaded and instantiated plugin
            instances. Specs that fail to load are omitted.
    """
    plugins: List[ChannelPlugin] = []
    for spec in specs:
        try:
            cls = _load_class(spec)
            plugin = cls()
            name = getattr(plugin, "name", None) or cls.__name__
            logger.info(f"Loaded channel plugin '{name}' from {spec!r}")
            plugins.append(plugin)
        except Exception as exc:
            logger.error(f"Failed to load channel plugin {spec!r}: {exc}")
    return plugins


def discover_entry_points() -> List[ChannelPlugin]:
    """Discover channel plugins via ``importlib.metadata`` entry points.

    Any installed package that declares an entry point under the group
    ``pyclaw.channels`` will be loaded. Errors are logged and skipped so
    that a broken entry point does not prevent other plugins from loading.

    Returns:
        List[ChannelPlugin]: Successfully discovered and instantiated plugin
            instances. Entry points that fail to load are omitted.
    """
    plugins: List[ChannelPlugin] = []
    try:
        from importlib.metadata import entry_points
        eps = entry_points(group=_ENTRY_POINT_GROUP)
    except Exception as exc:
        logger.warning(f"Entry point discovery failed: {exc}")
        return plugins

    for ep in eps:
        try:
            cls = ep.load()
            if not (isinstance(cls, type) and issubclass(cls, ChannelPlugin)):
                logger.warning(
                    f"Entry point '{ep.name}' ({ep.value}) is not a "
                    "ChannelPlugin subclass — skipping"
                )
                continue
            plugin = cls()
            name = getattr(plugin, "name", None) or ep.name
            logger.info(
                f"Discovered channel plugin '{name}' via entry point "
                f"'{ep.name}' ({ep.value})"
            )
            plugins.append(plugin)
        except Exception as exc:
            logger.error(
                f"Failed to load channel plugin entry point "
                f"'{ep.name}': {exc}"
            )

    return plugins


def load_all(specs: List[str]) -> List[ChannelPlugin]:
    """Discover all channel plugins from entry points and explicit specs.

    Entry-point plugins are loaded first; explicit specs are appended.
    Duplicate plugin classes are de-duplicated so that the same class cannot
    appear twice (first occurrence wins).

    Args:
        specs (List[str]): Explicit ``"module:Class"`` strings from the
            ``plugins.channels`` section of the config file.

    Returns:
        List[ChannelPlugin]: Deduplicated list of plugin instances ready to
            be started by the gateway.
    """
    seen_classes: set = set()
    plugins: List[ChannelPlugin] = []

    for plugin in discover_entry_points() + load_from_specs(specs):
        cls = type(plugin)
        if cls in seen_classes:
            logger.debug(f"Skipping duplicate plugin class {cls.__name__}")
            continue
        seen_classes.add(cls)
        plugins.append(plugin)

    return plugins
