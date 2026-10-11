"""Fixed control/result enums using D04's bounded, cancellable framing."""

from ._transport import Channel, Client


class RuntimeChannel(Channel):
    operations = frozenset(("boot", "result", "finished", "output", "notices"))


class RuntimeClient(Client):
    pass
