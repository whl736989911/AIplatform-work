"""Unit half of the outbox dispatcher: the retry policy, without a database."""

from __future__ import annotations

from octop.infra.workbuddy.outbox import (
    DEFAULT_BACKOFF_SECONDS,
    MAX_BACKOFF_SECONDS,
    WorkBuddyOutboxDispatcher,
    backoff_seconds,
)


def test_backoff_doubles_from_the_base_and_stops_at_the_ceiling() -> None:
    """The wait after a failure grows, and it is bounded."""
    assert backoff_seconds(1, base_seconds=5) == 5
    assert backoff_seconds(2, base_seconds=5) == 10
    assert backoff_seconds(3, base_seconds=5) == 20
    assert backoff_seconds(50, base_seconds=5, max_seconds=900) == 900
    assert backoff_seconds(1) == DEFAULT_BACKOFF_SECONDS
    assert (
        backoff_seconds(99, base_seconds=60, max_seconds=MAX_BACKOFF_SECONDS) == MAX_BACKOFF_SECONDS
    )
    # A nonsensical configuration still waits rather than spinning.
    assert backoff_seconds(0, base_seconds=0, max_seconds=0) == 1


def test_no_publisher_means_nothing_is_marked_delivered() -> None:
    """An unconfigured dispatcher must not claim events it cannot deliver."""
    dispatcher = WorkBuddyOutboxDispatcher(object(), None)  # type: ignore[arg-type]

    assert dispatcher.publisher_configured is False
    outcome = dispatcher.dispatch_once()

    assert outcome.claimed == 0, outcome
    assert outcome.dispatched == 0, outcome
    assert outcome.worked is False
    # ``object()`` fails on any repository call, so reaching here also proves the
    # database was never touched.
    assert dispatcher.drain(limit=10).claimed == 0
