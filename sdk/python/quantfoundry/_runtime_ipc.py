"""Fixed control/result enums using D04's bounded, cancellable framing."""

from ._transport import Channel, Client


class RuntimeChannel(Channel):
    operations = frozenset(("boot", "calendar", "result", "finished", "output", "notices", "metrics"))


class RuntimeClient(Client):
    pass
