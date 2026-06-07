"""Resilient HTTP for official data feeds.

Implements the connection hygiene required for high-trust ingestion: bounded
timeouts, exponential backoff with jitter on transient failures, and a simple
per-host circuit breaker so a flaky upstream cannot stall the whole pipeline.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field

import httpx

from degreezeor.config import settings

log = logging.getLogger("degreezeor.http")

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class CircuitOpenError(RuntimeError):
    """Raised when a host's circuit breaker is open (too many recent failures)."""


@dataclass
class _Breaker:
    failures: int = 0
    opened_at: float | None = None
    threshold: int = 5
    cooldown_s: float = 30.0

    def allow(self) -> bool:
        if self.opened_at is None:
            return True
        if time.monotonic() - self.opened_at >= self.cooldown_s:
            self.failures = 0
            self.opened_at = None
            return True
        return False

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()


@dataclass
class HttpClient:
    timeout: float = field(default_factory=lambda: settings.http_timeout_seconds)
    max_retries: int = field(default_factory=lambda: settings.http_max_retries)
    _breakers: dict[str, _Breaker] = field(default_factory=dict)

    def _breaker(self, host: str) -> _Breaker:
        return self._breakers.setdefault(host, _Breaker())

    def get_bytes(self, url: str, *, params: dict | None = None, headers: dict | None = None) -> bytes:
        host = httpx.URL(url).host or url
        breaker = self._breaker(host)
        if not breaker.allow():
            raise CircuitOpenError(f"circuit open for host {host!r}")

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = httpx.get(url, params=params, headers=headers, timeout=self.timeout)
                if resp.status_code in _RETRYABLE_STATUS:
                    raise httpx.HTTPStatusError(
                        f"retryable status {resp.status_code}", request=resp.request, response=resp
                    )
                resp.raise_for_status()
                breaker.record_success()
                return resp.content
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                breaker.record_failure()
                if attempt < self.max_retries:
                    backoff = (2**attempt) + random.uniform(0, 0.5)
                    log.warning("GET %s failed (%s); retry in %.1fs", url, exc, backoff)
                    time.sleep(backoff)
        assert last_exc is not None
        raise last_exc


# Module-level shared client (keeps breaker state across adapters).
client = HttpClient()
