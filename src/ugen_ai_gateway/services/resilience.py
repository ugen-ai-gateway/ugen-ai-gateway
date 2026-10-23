import time

import openai


def is_outage(exc: BaseException) -> bool:
    """Whether a provider error means the provider is unavailable right now.

    Connection failures, timeouts, rate limits and 5xx responses qualify; a
    rejected request (4xx) does not, because another provider would most
    likely reject it too.
    """
    if isinstance(exc, TimeoutError | openai.APIConnectionError):
        return True
    if isinstance(exc, openai.APIStatusError):
        return exc.status_code == 429 or exc.status_code >= 500
    return False


class CircuitBreaker:
    """Stop calling a provider after repeated outages, then probe it again.

    After ``failure_threshold`` consecutive outages the circuit opens and calls
    are refused for ``cooldown_seconds``. The next call after that is a trial:
    success closes the circuit, another outage opens it again.
    """

    def __init__(self, *, failure_threshold: int, cooldown_seconds: float) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if time.monotonic() - self._opened_at >= self._cooldown_seconds:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        return self.state != "open"

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self.state == "half_open" or self._failures >= self._failure_threshold:
            self._opened_at = time.monotonic()
