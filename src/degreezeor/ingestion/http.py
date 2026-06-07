"""Resilient HTTP for official data feeds.

Implements the connection hygiene required for high-trust ingestion: bounded
timeouts, exponential backoff with jitter on transient failures, and a simple
per-host circuit breaker so a flaky upstream cannot stall the whole pipeline.
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import time
from dataclasses import dataclass, field

import httpx

from degreezeor.config import settings

log = logging.getLogger("degreezeor.http")

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# Query params that are secrets (must never enter the cache key or be persisted).
_SECRET_PARAMS = {"api_key", "registrationkey", "key"}


def _cache_key(url: str, params: dict | None) -> str:
    safe = {k: v for k, v in (params or {}).items() if k.lower() not in _SECRET_PARAMS}
    payload = url + "?" + "&".join(f"{k}={safe[k]}" for k in sorted(safe))
    return hashlib.sha256(payload.encode()).hexdigest()


def _cache_dir():
    d = settings.data_dir / "http_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


class CircuitOpenError(RuntimeError):
    """Raised when a host's circuit breaker is open (too many recent failures)."""


class RetryableContentError(RuntimeError):
    """A 200 response whose *body* indicates a transient failure (e.g. an API that
    reports rate-limiting in the payload rather than via HTTP status)."""


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

    def get_bytes(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        validate: object | None = None,
    ) -> bytes:
        """Fetch bytes with retry/backoff, a circuit breaker, and a URL-keyed replay cache.

        ``validate(content) -> bool`` optionally checks the *body* (for APIs that signal
        rate-limiting in a 200 payload); invalid bodies are retried with backoff and are
        NEVER cached. Cache behavior: DZ_HTTP_CACHE=1 => cache-first (offline/replay);
        otherwise network-first, persist valid responses, fall back to cache on failure.
        """
        cache_path = _cache_dir() / _cache_key(url, params)
        cache_first = os.environ.get("DZ_HTTP_CACHE") == "1"
        if cache_first and cache_path.exists():
            return cache_path.read_bytes()

        host = httpx.URL(url).host or url
        breaker = self._breaker(host)
        if not breaker.allow():
            if cache_path.exists():
                log.warning("circuit open for %s; serving cached response", host)
                return cache_path.read_bytes()
            raise CircuitOpenError(f"circuit open for host {host!r}")

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = httpx.get(
                    url, params=params, headers=headers, timeout=self.timeout, follow_redirects=True
                )
                if resp.status_code in _RETRYABLE_STATUS:
                    raise httpx.HTTPStatusError(
                        f"retryable status {resp.status_code}", request=resp.request, response=resp
                    )
                resp.raise_for_status()
                if validate is not None and not validate(resp.content):
                    raise RetryableContentError("response body failed validation")
                breaker.record_success()
                cache_path.write_bytes(resp.content)  # persist only validated responses
                return resp.content
            except httpx.HTTPStatusError as exc:
                # Only transient statuses are retried; client errors (400/404/...) fail fast.
                if exc.response is not None and exc.response.status_code not in _RETRYABLE_STATUS:
                    breaker.record_success()  # server reachable; this is a request-level error
                    raise
                last_exc = exc
                breaker.record_failure()
                if attempt < self.max_retries:
                    backoff = (2**attempt) + random.uniform(0, 0.5)
                    log.warning("GET %s failed (%s); retry in %.1fs", url, exc, backoff)
                    time.sleep(backoff)
            except (httpx.TransportError, RetryableContentError) as exc:
                last_exc = exc
                breaker.record_failure()
                if attempt < self.max_retries:
                    backoff = (2**attempt) + random.uniform(0, 0.5)
                    log.warning("GET %s failed (%s); retry in %.1fs", url, exc, backoff)
                    time.sleep(backoff)
        # Network/validation exhausted: fall back to a cached (validated) copy if present.
        if cache_path.exists():
            log.warning("GET %s failed after retries; serving cached response", url)
            return cache_path.read_bytes()
        assert last_exc is not None
        raise last_exc


# Module-level shared client (keeps breaker state across adapters).
client = HttpClient()
