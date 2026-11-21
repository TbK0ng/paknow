"""Short-lived, process-local candidate evidence previews; never a review ledger."""
from collections import OrderedDict
from copy import deepcopy
from secrets import token_hex
from threading import Lock
from time import monotonic


class EvidencePreviews:
    def __init__(self, *, capacity: int = 64, ttl_seconds: float = 900):
        self.capacity = capacity
        self.ttl_seconds = ttl_seconds
        self._items: OrderedDict[str, tuple[float, str, dict]] = OrderedDict()
        self._lock = Lock()

    def put(self, fingerprint: str, packet: dict) -> str:
        token = token_hex(16)
        with self._lock:
            self._items[token] = (monotonic() + self.ttl_seconds, fingerprint, deepcopy(packet))
            while len(self._items) > self.capacity:
                self._items.popitem(last=False)
        return token

    def matching(self, token: str, fingerprint: str, generation: str) -> dict | None:
        with self._lock:
            item = self._items.get(token)
            if item is None:
                return None
            expires, key, packet = item
            if expires <= monotonic():
                del self._items[token]
                return None
            if key != fingerprint or packet.get("corpus_generation") != generation:
                return None
            return deepcopy(packet)
