import hashlib
import time
from collections import OrderedDict

from ugen_ai_gateway.analysis.schemas import RequestAnalysis


class AnalysisCache:
    """Bounded in-memory LRU cache of analysis results with a time-to-live.

    Keys are SHA-256 hashes of the request text. Cached results can include
    personal-data values reported by the analysis model, so they never leave
    this process and expire after ``ttl_seconds``.
    """

    def __init__(self, *, max_size: int, ttl_seconds: float) -> None:
        self._max_size = max_size
        self._ttl_seconds = ttl_seconds
        self._entries: OrderedDict[str, tuple[float, RequestAnalysis]] = OrderedDict()

    @staticmethod
    def key(user_input: str) -> str:
        return hashlib.sha256(user_input.encode()).hexdigest()

    def get(self, user_input: str) -> RequestAnalysis | None:
        if self._max_size <= 0:
            return None
        key = self.key(user_input)
        entry = self._entries.get(key)
        if entry is None:
            return None
        stored_at, analysis = entry
        if time.monotonic() - stored_at > self._ttl_seconds:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return analysis

    def put(self, user_input: str, analysis: RequestAnalysis) -> None:
        if self._max_size <= 0:
            return
        key = self.key(user_input)
        self._entries[key] = (time.monotonic(), analysis)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_size:
            self._entries.popitem(last=False)
