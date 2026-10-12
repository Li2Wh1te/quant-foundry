"""Fixed control/result enums using D04's bounded, cancellable framing."""

from ._transport import Channel, Client
from threading import Lock


class RuntimeChannel(Channel):
    operations = frozenset(("boot", "calendar", "result", "finished", "output", "notices", "metrics", "progress"))


class RuntimeClient(Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._request_lock = Lock()

    def call(self, *args, **kwargs):
        with self._request_lock:
            return super().call(*args, **kwargs)
