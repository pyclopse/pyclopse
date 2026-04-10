"""Stable module path for channel plugin test fixtures."""
from pyclopse.channels.plugin import ChannelPlugin, GatewayHandle
from pyclopse.channels.base import MessageTarget


class EchoPlugin(ChannelPlugin):
    name = "echo"

    def __init__(self):
        self.started = False
        self.stopped = False
        self.gateway_handle = None
        self.sent: list = []

    async def start(self, gateway: GatewayHandle) -> None:
        self.started = True
        self.gateway_handle = gateway

    async def stop(self) -> None:
        self.stopped = True

    async def send_message(self, target: MessageTarget, text: str,
                           parse_mode=None, **kwargs) -> None:
        self.sent.append((target.user_id, text))

    # Keep legacy send for backward-compat tests
    async def send(self, user_id: str, text: str, **kwargs) -> None:
        self.sent.append((user_id, text))


class AnotherPlugin(EchoPlugin):
    name = "another"
