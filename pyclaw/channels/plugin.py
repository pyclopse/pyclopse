"""
ChannelPlugin — base class for gateway-integrated channel plugins.

A channel plugin is a lightweight adapter that connects a messaging platform
to the pyclaw gateway.  It has three responsibilities:

1. **Lifecycle** — ``start(gateway)`` and ``stop()`` called by the gateway.
2. **Inbound** — receive messages from the platform and call
   ``gateway.dispatch(...)`` to deliver them to the agent.
3. **Outbound** — implement ``send(user_id, text, **kwargs)`` so the gateway
   (or other code) can send replies back to the platform.

Minimal example::

    from pyclaw.channels.plugin import ChannelPlugin, GatewayHandle

    class MyPlugin(ChannelPlugin):
        name = "myplugin"

        async def start(self, gateway: GatewayHandle) -> None:
            self._gw = gateway
            # connect to platform, start polling / webhook, etc.

        async def stop(self) -> None:
            pass  # tear down connections

        async def send(self, user_id: str, text: str, **kwargs) -> None:
            # send text to the platform user identified by user_id
            ...

Plugin registration
-------------------
Plugins are discovered in two ways (tried in order):

1. **Entry points** — any installed package that declares::

       [project.entry-points."pyclaw.channels"]
       myplugin = "mypackage.plugin:MyPlugin"

2. **Explicit config** — ``plugins.channels`` list in ``pyclaw.yaml``::

       plugins:
         channels:
           - mypackage.plugin:MyPlugin
"""

from abc import ABC, abstractmethod
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    pass


class GatewayHandle:
    """
    Narrow gateway interface given to channel plugins.

    Plugins call :meth:`dispatch` to deliver an inbound message to the agent
    and receive the response back.

    This is a concrete class backed by the real gateway at runtime, but tests
    can substitute a simple mock.
    """

    async def dispatch(
        self,
        channel: str,
        user_id: str,
        user_name: str,
        text: str,
        message_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        Deliver an inbound message to the gateway and return the agent reply.

        Parameters
        ----------
        channel:
            Channel identifier, e.g. ``"discord"``.
        user_id:
            Platform user ID (string).
        user_name:
            Human-readable user name for display.
        text:
            Message content.
        message_id:
            Optional platform message ID (used for deduplication).

        Returns
        -------
        str or None:
            Agent reply text, or ``None`` if no response was generated.
        """
        raise NotImplementedError  # replaced by _GatewayHandleImpl at runtime


class ChannelPlugin(ABC):
    """
    Abstract base class for gateway-integrated channel plugins.

    Subclass this to add a new messaging channel.  Override
    :meth:`start`, :meth:`stop`, and :meth:`send`.
    """

    # Class-level default name — subclasses can override as a class attribute
    # or as a property.  The loader uses this to register the plugin.
    name: str = ""

    @abstractmethod
    async def start(self, gateway: GatewayHandle) -> None:
        """
        Start the channel.

        Called by the gateway during :meth:`~pyclaw.core.gateway.Gateway.initialize`.
        Establish connections, start polling loops, register webhooks, etc.

        Parameters
        ----------
        gateway:
            Handle to use for dispatching inbound messages.
        """

    @abstractmethod
    async def stop(self) -> None:
        """
        Stop the channel.

        Called by the gateway during :meth:`~pyclaw.core.gateway.Gateway.stop`.
        Cancel polling tasks, close connections, deregister webhooks, etc.
        """

    @abstractmethod
    async def send(self, user_id: str, text: str, **kwargs) -> None:
        """
        Send a message to a user on this channel.

        Parameters
        ----------
        user_id:
            Platform user ID as returned to the ``dispatch`` call.
        text:
            Message content to send.
        **kwargs:
            Platform-specific extras (e.g. ``thread_ts`` for Slack,
            ``parse_mode`` for Telegram).
        """
