"""A polite HTTP client shared by providers.

* Minimum interval between requests (per provider).
* Exponential backoff for 429 / 5xx / network errors, honouring Retry-After.
* Optional response cache.
* Bot-challenge detection: a CAPTCHA, challenge page or access-denied response
  raises ProviderBlockedError immediately. We never retry it and never try to
  bypass it — the affected provider is paused and the problem is logged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Any, Callable, Protocol

import httpx

from app.core.errors import (
    ProviderAuthError,
    ProviderBlockedError,
    ProviderRateLimitedError,
    ProviderRequestError,
    ProviderTemporaryError,
)

log = logging.getLogger(__name__)

USER_AGENT = "FlightRadar/0.1 (personal price monitor; low volume)"

CHALLENGE_MARKERS = (
    "captcha",
    "cf-chl",
    "challenge-platform",
    "cf-browser-verification",
    "attention required",
    "access denied",
    "are you a robot",
    "px-captcha",
    "datadome",
    "perimeterx",
    "bot detection",
    "unusual traffic",
)


def looks_like_bot_challenge(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "")
    if "html" not in content_type and response.status_code not in (403, 429, 503):
        return False
    try:
        body = response.text[:20000].lower()
    except Exception:  # pragma: no cover - undecodable body
        return False
    return any(marker in body for marker in CHALLENGE_MARKERS)


class ResponseCache(Protocol):
    def get(self, key: str) -> Any | None: ...
    def set(self, key: str, provider: str, value: Any, ttl_seconds: float) -> None: ...


def cache_key(url: str, params: dict[str, Any] | None) -> str:
    raw = url + "?" + json.dumps(params or {}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


class PoliteHttpClient:
    def __init__(
        self,
        provider: str,
        *,
        min_interval_seconds: float = 1.5,
        max_retries: int = 3,
        backoff_base_seconds: float = 2.0,
        timeout_seconds: float = 20.0,
        cache: ResponseCache | None = None,
        cache_ttl_seconds: float = 0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.provider = provider
        self.min_interval = min_interval_seconds
        self.max_retries = max_retries
        self.backoff_base = backoff_base_seconds
        self.cache = cache
        self.cache_ttl = cache_ttl_seconds
        self._sleep = sleep
        self._last_request = 0.0
        self._lock = threading.Lock()
        self.requests_made = 0
        self.cache_hits = 0
        self._client = httpx.Client(
            timeout=timeout_seconds,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def _wait_turn(self) -> None:
        with self._lock:
            elapsed = time.monotonic() - self._last_request
            if self._last_request and elapsed < self.min_interval:
                self._sleep(self.min_interval - elapsed)
            self._last_request = time.monotonic()

    def _backoff(self, attempt: int, response: httpx.Response | None = None) -> float:
        if response is not None:
            retry_after = response.headers.get("retry-after")
            if retry_after and retry_after.isdigit():
                return min(float(retry_after), 300.0)
        return self.backoff_base * (2**attempt)

    def visit(self, url: str) -> int:
        """Load a normal page once (e.g. to receive the site's session cookies, as any
        browser would). Only a clear challenge/denial response counts as blocked here,
        because ordinary HTML pages often mention 'captcha' in their scripts."""
        self._wait_turn()
        self.requests_made += 1
        try:
            response = self._client.get(url, headers={"Accept": "text/html"})
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ProviderTemporaryError(f"{self.provider}: could not load {url}: {exc}") from exc
        if response.status_code in (403, 429, 503) and looks_like_bot_challenge(response):
            raise ProviderBlockedError(
                f"{self.provider}: bot challenge / access denied on {url} (HTTP {response.status_code})"
            )
        return response.status_code

    def get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        *,
        use_cache: bool = True,
    ) -> tuple[Any, bool]:
        """Return (parsed JSON, from_cache). Secrets must go in headers, not params,
        because params are used for the cache key and may be logged."""
        key = cache_key(url, params)
        if use_cache and self.cache is not None and self.cache_ttl > 0:
            cached = self.cache.get(key)
            if cached is not None:
                self.cache_hits += 1
                log.debug("[%s] cache hit for %s", self.provider, params)
                return cached, True

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._wait_turn()
            self.requests_made += 1
            try:
                response = self._client.get(url, params=params, headers=headers)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                wait = self._backoff(attempt)
                log.warning("[%s] network error (%s), attempt %d/%d, retrying in %.0fs",
                            self.provider, exc.__class__.__name__, attempt + 1, self.max_retries + 1, wait)
                if attempt < self.max_retries:
                    self._sleep(wait)
                continue

            if looks_like_bot_challenge(response):
                log.error("[%s] bot challenge / access denied (HTTP %s). Stopping this provider; "
                          "no bypass will be attempted.", self.provider, response.status_code)
                raise ProviderBlockedError(
                    f"{self.provider}: CAPTCHA / bot challenge / access denied (HTTP {response.status_code})"
                )
            status = response.status_code
            if status == 401:
                raise ProviderAuthError(f"{self.provider}: credentials rejected (HTTP 401)")
            if status == 403:
                if "json" in response.headers.get("content-type", ""):
                    # An API saying "forbidden" in JSON is a permissions/token problem.
                    raise ProviderAuthError(f"{self.provider}: access forbidden (HTTP 403): {response.text[:200]}")
                raise ProviderBlockedError(f"{self.provider}: access denied (HTTP 403)")
            if status == 429 or status >= 500:
                last_error = (ProviderRateLimitedError if status == 429 else ProviderTemporaryError)(
                    f"{self.provider}: HTTP {status}"
                )
                wait = self._backoff(attempt, response)
                log.warning("[%s] HTTP %s, attempt %d/%d, retrying in %.0fs",
                            self.provider, status, attempt + 1, self.max_retries + 1, wait)
                if attempt < self.max_retries:
                    self._sleep(wait)
                continue
            if status >= 400:
                raise ProviderRequestError(f"{self.provider}: HTTP {status}: {response.text[:300]}", status)
            try:
                data = response.json()
            except ValueError as exc:
                raise ProviderRequestError(f"{self.provider}: response is not JSON") from exc
            if self.cache is not None and self.cache_ttl > 0:
                self.cache.set(key, self.provider, data, self.cache_ttl)
            return data, False

        if isinstance(last_error, (ProviderRateLimitedError, ProviderTemporaryError)):
            raise last_error
        raise ProviderTemporaryError(f"{self.provider}: request failed after retries: {last_error}")
