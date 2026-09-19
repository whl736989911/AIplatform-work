"""Rate-limit policy, headers and the fail-closed rule (contract 6.7).

The counting window is supplied by a store, so these tests exercise the policy
without a Redis server; ``RedisWindowStore`` itself is covered by the deployment
probe and by the integration run where ``REDIS_URL`` points at a live instance.
"""

from __future__ import annotations

from typing import Any

import pytest

from octop.infra.workbuddy.ratelimit import (
    ADMIN_LIMIT,
    BUSINESS_LIMIT,
    DEFAULT_LIMITS,
    KNOWLEDGE_SEARCH_LIMIT,
    TENANT_LIMIT,
    WORKFLOW_EXECUTE_LIMIT,
    RateLimit,
    RateLimitUnavailable,
    SlidingWindowLimiter,
    resolve_window_store,
)


class InMemoryWindowStore:
    """The same sliding-window semantics as the Redis script, for tests only."""

    def __init__(self) -> None:
        self._marks: dict[str, list[int]] = {}

    def admit(self, key: str, *, now_ms: int, limit: int, window_ms: int) -> int:
        marks = [mark for mark in self._marks.get(key, []) if mark > now_ms - window_ms]
        marks.append(now_ms)
        self._marks[key] = marks
        return len(marks)


def test_published_limits_match_the_contract() -> None:
    """The numbers are the contract's, not a local preference."""
    assert DEFAULT_LIMITS[BUSINESS_LIMIT] == RateLimit(BUSINESS_LIMIT, 100, 60)
    assert DEFAULT_LIMITS[WORKFLOW_EXECUTE_LIMIT] == RateLimit(
        WORKFLOW_EXECUTE_LIMIT, 20, 60, fail_closed=True
    )
    assert DEFAULT_LIMITS[TENANT_LIMIT] == RateLimit(TENANT_LIMIT, 1000, 60)
    assert DEFAULT_LIMITS[ADMIN_LIMIT] == RateLimit(ADMIN_LIMIT, 30, 60, fail_closed=True)
    assert DEFAULT_LIMITS[KNOWLEDGE_SEARCH_LIMIT] == RateLimit(KNOWLEDGE_SEARCH_LIMIT, 60, 60)


def test_a_subject_gets_its_allowance_and_then_a_retry_hint() -> None:
    limiter = SlidingWindowLimiter(InMemoryWindowStore())
    now = 1_700_000_000.0
    for _ in range(20):
        decision = limiter.check(WORKFLOW_EXECUTE_LIMIT, "user:1", now=now)
        assert decision.allowed is True
    refused = limiter.check(WORKFLOW_EXECUTE_LIMIT, "user:1", now=now)
    assert refused.allowed is False
    assert refused.remaining == 0
    assert refused.headers()["Retry-After"] == str(refused.retry_after)
    assert refused.retry_after >= 1
    assert refused.headers()["X-RateLimit-Limit"] == "20"


def test_the_window_slides_and_subjects_are_independent() -> None:
    limiter = SlidingWindowLimiter(InMemoryWindowStore())
    start = 1_700_000_000.0
    for _ in range(20):
        limiter.check(WORKFLOW_EXECUTE_LIMIT, "user:1", now=start)
    assert limiter.check(WORKFLOW_EXECUTE_LIMIT, "user:1", now=start).allowed is False

    # Another subject has its own allowance.
    assert limiter.check(WORKFLOW_EXECUTE_LIMIT, "user:2", now=start).allowed is True

    # Once the first request falls out of the window the subject may run again.
    later = start + 61
    assert limiter.check(WORKFLOW_EXECUTE_LIMIT, "user:1", now=later).allowed is True


def test_headers_report_the_remaining_allowance() -> None:
    limiter = SlidingWindowLimiter(InMemoryWindowStore())
    decision = limiter.check(BUSINESS_LIMIT, "user:7", now=1_700_000_000.0)
    headers = decision.headers()
    assert headers["X-RateLimit-Limit"] == "100"
    assert headers["X-RateLimit-Remaining"] == "99"
    assert headers["X-RateLimit-Reset"] == "1700000060"
    assert "Retry-After" not in headers


def test_sensitive_policies_fail_closed() -> None:
    limiter = SlidingWindowLimiter(InMemoryWindowStore())
    # Runs, external writes and admin operations may not proceed unlimited.
    assert limiter.fail_closed(WORKFLOW_EXECUTE_LIMIT) is True
    assert limiter.fail_closed(ADMIN_LIMIT) is True
    # Ordinary reads and searches degrade rather than refuse.
    assert limiter.fail_closed(BUSINESS_LIMIT) is False
    assert limiter.fail_closed(KNOWLEDGE_SEARCH_LIMIT) is False


def test_an_unknown_policy_is_a_programming_error() -> None:
    limiter = SlidingWindowLimiter(InMemoryWindowStore())
    with pytest.raises(KeyError):
        limiter.check("not_a_policy", "user:1")


def test_store_failures_are_reported_as_unavailable() -> None:
    class BrokenStore:
        def admit(self, key: str, *, now_ms: int, limit: int, window_ms: int) -> int:
            raise RateLimitUnavailable("the store is gone")

    limiter = SlidingWindowLimiter(BrokenStore())
    with pytest.raises(RateLimitUnavailable):
        limiter.check(WORKFLOW_EXECUTE_LIMIT, "user:1")


def test_no_redis_url_means_no_store() -> None:
    assert resolve_window_store({}) is None
    assert resolve_window_store({"REDIS_URL": "   "}) is None


def test_redis_url_resolves_to_the_shared_store(monkeypatch: pytest.MonkeyPatch) -> None:
    created: dict[str, Any] = {}

    class FakeRedis:
        @classmethod
        def from_url(cls, url: str, **kwargs: Any) -> Any:
            created["url"] = url
            created["kwargs"] = kwargs

            def register_script(script: str) -> Any:
                created["script"] = script
                return lambda keys, args: 1

            instance = type("Client", (), {})()
            instance.register_script = register_script
            return instance

    import sys
    import types

    module = types.ModuleType("redis")
    module.Redis = FakeRedis  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "redis", module)

    store = resolve_window_store({"REDIS_URL": "redis://cache:6379/0"})
    assert store is not None
    assert created["url"] == "redis://cache:6379/0"
    assert store.admit("key", now_ms=1, limit=1, window_ms=1000) == 1
    assert "ZREMRANGEBYSCORE" in created["script"]
