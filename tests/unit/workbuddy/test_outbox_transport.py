"""The outbox's transport entity (A-23).

The dispatcher was already complete — claim, retry with backoff, dead-letter —
but nothing implemented the port it publishes through, so a deployment wired to
this repository had no way to actually deliver an event. These tests pin what the
Redis transport promises: the event lands on its topic's queue with its own id
inside, a transport refusal reaches the dispatcher as a coded failure (which is
what becomes the dead letter's recorded error), and an unconfigured deployment
resolves to *no* publisher rather than to one that pretends to deliver.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from octop.infra.db.repos.workbuddy_runtime import OutboxRow
from octop.infra.workbuddy.outbox import (
    OutboxDeliveryFailed,
    RedisOutboxPublisher,
    resolve_redis_publisher,
)


class _FakeRedis:
    """Records what would have been pushed, and can refuse like a real one."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self._fail = fail

    def lpush(self, key: str, body: str) -> None:
        if self._fail:
            raise ConnectionError("redis is down")
        self.calls.append((key, body))


def _event(**overrides: Any) -> OutboxRow:
    base: dict[str, Any] = {
        "id": "ev-1",
        "topic": "workbuddy.execution.finished",
        "dedupe_key": "exec-1",
        "payload": {"execution_id": "exec-1", "status": "succeeded"},
        "status": "pending",
        "attempts": 3,
    }
    return OutboxRow(**{**base, **overrides})


def test_an_event_lands_on_its_topic_queue_carrying_its_own_id() -> None:
    client = _FakeRedis()
    RedisOutboxPublisher(client=client).publish(_event())
    assert len(client.calls) == 1, client.calls
    key, body = client.calls[0]
    assert key == "workbuddy:outbox:workbuddy.execution.finished", key
    decoded = json.loads(body)
    # The id travels inside the body: that is what lets a consumer de-duplicate
    # against PostgreSQL instead of trusting the channel's delivery count.
    assert decoded == {
        "id": "ev-1",
        "topic": "workbuddy.execution.finished",
        "dedupe_key": "exec-1",
        "attempt": 3,
        "payload": {"execution_id": "exec-1", "status": "succeeded"},
    }, decoded


def test_a_transport_refusal_reaches_the_dispatcher_as_a_coded_failure() -> None:
    publisher = RedisOutboxPublisher(client=_FakeRedis(fail=True))
    with pytest.raises(OutboxDeliveryFailed) as raised:
        publisher.publish(_event())
    # The dispatcher records ``code: message`` as the dead letter's last error, so
    # the code has to be stable enough to read months later.
    assert raised.value.code == "OUTBOX_TRANSPORT_FAILED", raised.value.code
    assert "redis is down" in raised.value.message, raised.value.message


def test_an_unconfigured_deployment_gets_no_publisher_rather_than_a_fake_one() -> None:
    assert resolve_redis_publisher({}) is None
    assert resolve_redis_publisher({"REDIS_URL": "   "}) is None
    # And a configured one is the real thing (the client connects lazily, so this
    # builds a publisher without touching the network).
    publisher = resolve_redis_publisher({"REDIS_URL": "redis://127.0.0.1:6379/0"})
    assert isinstance(publisher, RedisOutboxPublisher), publisher
