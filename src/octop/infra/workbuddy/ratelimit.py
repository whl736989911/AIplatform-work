"""Sliding-window rate limiting for the WorkBuddy API surface.

The contract fixes both the limits and the client-visible behaviour:

* a Redis sliding window is the shared state, because every worker must see the
  same counters (contract 6.7);
* a refused request answers ``429 RATE_LIMITED`` with ``Retry-After`` and the
  ``X-RateLimit-*`` headers, and a client that receives 409/422 must not retry
  exponentially;
* when the limiter's dependency is gone, the identity entry points, external
  writes and expensive requests are refused with ``DEPENDENCY_UNAVAILABLE``
  rather than let through -- losing the limiter must not silently relax the
  protection it provides.

The counting itself lives behind :class:`WindowStore` so the policy, the headers
and the fail-closed rule stay testable without a Redis server, while the Redis
implementation keeps the trim-and-count in one atomic script.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

__all__ = [
    "ADMIN_LIMIT",
    "BUSINESS_LIMIT",
    "DEFAULT_LIMITS",
    "KNOWLEDGE_SEARCH_LIMIT",
    "RATE_LIMIT_HEADERS",
    "RateLimit",
    "RateLimitDecision",
    "RateLimitUnavailable",
    "RedisWindowStore",
    "SlidingWindowLimiter",
    "TENANT_LIMIT",
    "WINDOW_STORE_UNAVAILABLE",
    "WORKFLOW_EXECUTE_LIMIT",
    "resolve_window_store",
]

WINDOW_STORE_UNAVAILABLE = "RATE_LIMIT_STORE_UNAVAILABLE"

# The contract's published limits, per subject and window (contract 6.7).
BUSINESS_LIMIT = "business"
WORKFLOW_EXECUTE_LIMIT = "workflow_execute"
TENANT_LIMIT = "tenant"
ADMIN_LIMIT = "admin"
KNOWLEDGE_SEARCH_LIMIT = "knowledge_search"

RATE_LIMIT_HEADERS = ("X-RateLimit-Remaining", "X-RateLimit-Reset", "Retry-After")


@dataclass(frozen=True, slots=True)
class RateLimit:
    """One subject's allowance: ``limit`` requests per ``window_seconds``."""

    name: str
    limit: int
    window_seconds: int
    # Sensitive entries must refuse to run when the limiter is unavailable: an
    # identity check, an external write or an expensive run may not proceed
    # unlimited just because the store is down.
    fail_closed: bool = False

    def window_ms(self) -> int:
        return max(int(self.window_seconds), 1) * 1000


DEFAULT_LIMITS: Mapping[str, RateLimit] = {
    # 100 req/min/user over the ordinary business API.
    BUSINESS_LIMIT: RateLimit(BUSINESS_LIMIT, limit=100, window_seconds=60),
    # 20 new workflow runs per minute per actor key; a run is an external-write
    # candidate and is expensive, so it fails closed.
    WORKFLOW_EXECUTE_LIMIT: RateLimit(
        WORKFLOW_EXECUTE_LIMIT, limit=20, window_seconds=60, fail_closed=True
    ),
    # 1000 req/min for everything a tenant does, however it is distributed.
    TENANT_LIMIT: RateLimit(TENANT_LIMIT, limit=1000, window_seconds=60),
    # 30 admin operations per minute on the separate management surface.
    ADMIN_LIMIT: RateLimit(ADMIN_LIMIT, limit=30, window_seconds=60, fail_closed=True),
    # 60 knowledge-base searches per minute per user.
    KNOWLEDGE_SEARCH_LIMIT: RateLimit(KNOWLEDGE_SEARCH_LIMIT, limit=60, window_seconds=60),
}


class RateLimitUnavailable(RuntimeError):
    """The shared window store could not answer, so nothing can be counted."""


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    reset_at: int

    @property
    def retry_after(self) -> int:
        """Seconds a client should wait before retrying, at least one."""
        return max(int(self.reset_at - time.time()) + 1, 1)

    def headers(self) -> dict[str, str]:
        headers = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(self.remaining, 0)),
            "X-RateLimit-Reset": str(int(self.reset_at)),
        }
        if not self.allowed:
            headers["Retry-After"] = str(self.retry_after)
        return headers


class WindowStore(Protocol):
    """Counts requests inside a sliding window, atomically."""

    def admit(self, key: str, *, now_ms: int, limit: int, window_ms: int) -> int:
        """Record one attempt and return how many the window now holds.

        Every attempt is counted, including the ones that exceed the limit: a
        client that keeps hammering stays refused instead of slipping through
        once the first refusal is forgotten. A caller admits the request when the
        returned count is at most ``limit``.
        """
        ...


_ADMIT_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
redis.call('ZADD', key, now, ARGV[4])
local count = redis.call('ZCARD', key)
redis.call('PEXPIRE', key, window)
return count
"""


class RedisWindowStore:
    """Sliding window over a Redis sorted set, trimmed and counted atomically."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self._script = client.register_script(_ADMIT_SCRIPT)

    def admit(self, key: str, *, now_ms: int, limit: int, window_ms: int) -> int:
        try:
            count = self._script(
                keys=[key],
                # Every attempt needs its own member: two requests in the same
                # millisecond would otherwise update one score instead of adding
                # a second member, and a burst would count as one.
                args=[
                    int(now_ms),
                    int(window_ms),
                    int(limit),
                    f"{int(now_ms)}-{uuid.uuid4().hex}",
                ],
            )
        except Exception as exc:  # noqa: BLE001 - any driver failure is unavailable
            raise RateLimitUnavailable(f"rate-limit store failed: {type(exc).__name__}") from exc
        return int(count)


@dataclass(frozen=True, slots=True)
class SlidingWindowLimiter:
    """Applies one policy to one subject, counting in the shared window store."""

    store: WindowStore
    limits: Mapping[str, RateLimit] = field(default_factory=lambda: dict(DEFAULT_LIMITS))

    def check(self, policy: str, subject: str, *, now: float | None = None) -> RateLimitDecision:
        limit = self.limits.get(policy)
        if limit is None:
            raise KeyError(f"unknown rate-limit policy {policy!r}")
        moment = float(now if now is not None else time.time())
        now_ms = int(moment * 1000)
        count = self.store.admit(
            f"ratelimit:{policy}:{subject}",
            now_ms=now_ms,
            limit=limit.limit,
            window_ms=limit.window_ms(),
        )
        return RateLimitDecision(
            allowed=count <= limit.limit,
            limit=limit.limit,
            remaining=max(limit.limit - count, 0),
            reset_at=int(moment) + limit.window_seconds,
        )

    def fail_closed(self, policy: str) -> bool:
        limit = self.limits.get(policy)
        return bool(limit and limit.fail_closed)


def resolve_window_store(environ: Mapping[str, str] | None = None) -> WindowStore | None:
    """The shared store from ``REDIS_URL``, or ``None`` when unconfigured."""
    import os

    source = environ if environ is not None else os.environ
    url = (source.get("REDIS_URL") or "").strip()
    if not url:
        return None
    try:
        import redis
    except ImportError as exc:  # pragma: no cover - the dependency is pinned
        raise RateLimitUnavailable("the redis client is not installed") from exc
    client = redis.Redis.from_url(url, socket_connect_timeout=3, socket_timeout=3)
    return RedisWindowStore(client)
