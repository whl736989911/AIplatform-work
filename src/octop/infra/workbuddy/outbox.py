"""WorkBuddy outbox dispatcher: the tier that publishes committed events.

The runtime commits an outbox row in the same transaction as the fact it
announces (§4.6.2: execution started, execution finished, waiting for
reconciliation, approval created). PostgreSQL is the source of truth and the
broker is only an at-least-once channel: the publish is recorded *after* it
succeeds, so a crash in between re-delivers and a consumer is expected to
de-duplicate against PostgreSQL state. What was missing is the *other* half --
nothing ever read those rows, so a committed event stayed ``pending`` forever.

This module owns that half and nothing else:

* a claim is one short transaction that takes the oldest due events and counts an
  attempt on each, so two dispatchers never publish the same row;
* a successful publish marks the row ``dispatched``;
* a failed publish reschedules it with a bounded exponential backoff, and after
  the attempt ceiling the row is dead-lettered (``failed`` with the last error)
  instead of being retried forever;
* the transport itself is a port. A deployment injects its publisher (Redis, or
  anything else); a process with no publisher refuses to run rather than mark
  events delivered that nobody received.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos.workbuddy_runtime import OutboxRow, WorkBuddyRuntimeRepo

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 50
DEFAULT_VISIBILITY_TIMEOUT_SECONDS = 60
DEFAULT_MAX_ATTEMPTS = 8
DEFAULT_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 900
DEFAULT_IDLE_SLEEP_SECONDS = 0.5


@runtime_checkable
class OutboxPublisher(Protocol):
    """The delivery channel a deployment wires in (contract: Redis, at least once)."""

    def publish(self, event: OutboxRow) -> None: ...


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    """What one pass over the due events did."""

    claimed: int = 0
    dispatched: int = 0
    rescheduled: int = 0
    dead_lettered: int = 0

    @property
    def worked(self) -> bool:
        return self.claimed > 0


def backoff_seconds(
    attempts: int,
    *,
    base_seconds: int = DEFAULT_BACKOFF_SECONDS,
    max_seconds: int = MAX_BACKOFF_SECONDS,
) -> int:
    """Exponential backoff for the attempt that just failed, capped.

    ``attempts`` counts attempts already made, so the first failure waits
    ``base_seconds`` and the wait doubles from there.
    """
    base = max(1, int(base_seconds))
    steps = max(0, int(attempts) - 1)
    if steps >= 32:  # a sanity ceiling: 2**steps must stay a sane integer
        return max(1, int(max_seconds))
    return int(max(1, min(base * (2**steps), int(max_seconds))))


class WorkBuddyOutboxDispatcher:
    """Publishes committed outbox events, once each, with bounded retries."""

    def __init__(
        self,
        db: DatabasePool,
        publisher: OutboxPublisher | None,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        base_backoff_seconds: int = DEFAULT_BACKOFF_SECONDS,
        max_backoff_seconds: int = MAX_BACKOFF_SECONDS,
        idle_sleep_seconds: float = DEFAULT_IDLE_SLEEP_SECONDS,
        visibility_timeout_seconds: int = DEFAULT_VISIBILITY_TIMEOUT_SECONDS,
        dispatcher_id: str = "workbuddy-outbox",
    ) -> None:
        self._repo = WorkBuddyRuntimeRepo(db)
        self._publisher = publisher
        self._batch_size = max(1, int(batch_size))
        self._max_attempts = max(1, int(max_attempts))
        self._base_backoff = max(1, int(base_backoff_seconds))
        self._max_backoff = max(1, int(max_backoff_seconds))
        self._idle_sleep = float(idle_sleep_seconds)
        self._visibility_seconds = max(1, int(visibility_timeout_seconds))
        self.dispatcher_id = dispatcher_id
        self._task: asyncio.Task[None] | None = None
        self._stop: asyncio.Event | None = None

    @property
    def publisher_configured(self) -> bool:
        return self._publisher is not None

    def dispatch_once(self) -> DispatchOutcome:
        """Claim and publish one batch of due events.

        Without a configured publisher this refuses to run: marking events
        delivered that no channel received would be a lie about the outbox.
        """
        publisher = self._publisher
        if publisher is None:
            logger.warning(
                "workbuddy outbox dispatcher %s has no publisher configured; events stay pending",
                self.dispatcher_id,
            )
            return DispatchOutcome()
        events = self._repo.claim_due_outbox(
            limit=self._batch_size, visibility_seconds=self._visibility_seconds
        )
        dispatched = rescheduled = dead = 0
        for event in events:
            try:
                publisher.publish(event)
            except Exception as exc:  # noqa: BLE001 - any transport failure retries
                if self._reschedule(event, exc):
                    dead += 1
                else:
                    rescheduled += 1
                continue
            if self._repo.mark_outbox_dispatched(event.id):
                dispatched += 1
        return DispatchOutcome(
            claimed=len(events),
            dispatched=dispatched,
            rescheduled=rescheduled,
            dead_lettered=dead,
        )

    def drain(self, *, limit: int = 1000) -> DispatchOutcome:
        """Dispatch until nothing is due, and report the totals."""
        claimed = dispatched = rescheduled = dead = 0
        while claimed < limit:
            outcome = self.dispatch_once()
            if not outcome.worked:
                break
            claimed += outcome.claimed
            dispatched += outcome.dispatched
            rescheduled += outcome.rescheduled
            dead += outcome.dead_lettered
        return DispatchOutcome(
            claimed=claimed,
            dispatched=dispatched,
            rescheduled=rescheduled,
            dead_lettered=dead,
        )

    def _reschedule(self, event: OutboxRow, exc: Exception) -> bool:
        """Record the failed attempt; True when the event is now dead-lettered."""
        # The claim counted this attempt already.
        attempts = int(event.attempts)
        code = str(getattr(exc, "code", "") or type(exc).__name__)
        if attempts >= self._max_attempts:
            self._repo.dead_letter_outbox(event.id, error=f"{code}: {exc}"[:500])
            logger.error(
                "workbuddy outbox event %s (%s) is dead after %s attempts: %s",
                event.id,
                event.topic,
                attempts,
                exc,
            )
            return True
        delay = backoff_seconds(
            attempts, base_seconds=self._base_backoff, max_seconds=self._max_backoff
        )
        self._repo.reschedule_outbox(event.id, delay_seconds=delay, error=f"{code}: {exc}"[:500])
        logger.warning(
            "workbuddy outbox event %s (%s) failed on attempt %s; retrying in %ss: %s",
            event.id,
            event.topic,
            attempts,
            delay,
            exc,
        )
        return False

    async def start(self) -> None:
        """Serve the dispatch loop as a background task of this process."""
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop(self._stop))
        logger.info("workbuddy outbox dispatcher %s started", self.dispatcher_id)

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - shutdown is best effort
            logger.warning("workbuddy outbox dispatcher stopped with an error: %s", exc)
        self._stop = None

    async def _loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            outcome = await asyncio.to_thread(self.dispatch_once)
            if outcome.worked:
                continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._idle_sleep)
            except TimeoutError:
                continue


__all__ = [
    "DEFAULT_BACKOFF_SECONDS",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_IDLE_SLEEP_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_VISIBILITY_TIMEOUT_SECONDS",
    "MAX_BACKOFF_SECONDS",
    "DispatchOutcome",
    "OutboxPublisher",
    "WorkBuddyOutboxDispatcher",
    "backoff_seconds",
]
