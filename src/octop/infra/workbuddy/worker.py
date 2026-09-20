"""WorkBuddy execution worker: the tier that runs accepted executions.

The API accepts an execution and records it ``queued``; nothing runs inside the
request. This worker reads its work from the database (contract §8.1 -- the API
tier holds no in-process execution state, and the worker keeps no queue of its
own):

* a claim is one short transaction that locks the tenant's row, admits the
  execution against the tenant's concurrency ceiling, reserves the running slot
  and takes the execution lease with a monotonic fence;
* a crash is recovered by waiting for that lease to expire: the next claimant
  bumps the fence, so the dead worker can no longer commit anything;
* every attempt commits through the runtime service, which re-checks the fence
  inside the transaction that writes the result.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import uuid

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos.workbuddy_runtime import WorkBuddyRuntimeRepo
from octop.infra.errors import OctopError
from octop.infra.workbuddy.runtime import EXECUTION_RESERVATION_TTL_SECONDS, WorkBuddyRuntimeService

logger = logging.getLogger(__name__)

DEFAULT_LEASE_TTL_SECONDS = 600
DEFAULT_IDLE_SLEEP_SECONDS = 0.5
_WORKER_OFF = {"0", "off", "false", "no"}


def workbuddy_worker_enabled(db: object) -> bool:
    """Whether this process runs the execution worker.

    A single-process install (desktop, FnOS) hosts the worker beside the API; a
    deployment that runs the worker tier separately sets
    ``OCTOP_WORKBUDDY_WORKER=off`` on the API and starts ``octop
    workbuddy-worker`` instead. SQLite never has WorkBuddy work to run.
    """
    if getattr(db, "dialect", None) != "postgresql":
        return False
    return os.environ.get("OCTOP_WORKBUDDY_WORKER", "on").strip().lower() not in _WORKER_OFF


def default_worker_id() -> str:
    """Identity this process claims leases under (host, pid, one random suffix)."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class WorkBuddyExecutionWorker:
    """Claims admitted executions one at a time and runs them.

    ``run_once`` is synchronous because the runtime it drives is: the adapters,
    the database pool and the workflow engine block. A server hosts the loop in a
    thread so a long run cannot stall the event loop.
    """

    def __init__(
        self,
        db: DatabasePool,
        *,
        service: WorkBuddyRuntimeService | None = None,
        worker_id: str | None = None,
        lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
        reservation_ttl_seconds: int = EXECUTION_RESERVATION_TTL_SECONDS,
        idle_sleep_seconds: float = DEFAULT_IDLE_SLEEP_SECONDS,
    ) -> None:
        self._repo = WorkBuddyRuntimeRepo(db)
        self._service = service or WorkBuddyRuntimeService.for_control_plane(db)
        self.worker_id = worker_id or default_worker_id()
        self._lease_ttl_seconds = int(lease_ttl_seconds)
        self._reservation_ttl_seconds = int(reservation_ttl_seconds)
        self._idle_sleep = float(idle_sleep_seconds)
        self._task: asyncio.Task[None] | None = None
        self._stop: asyncio.Event | None = None

    def run_once(self) -> str | None:
        """Run one claimable execution, or return None when there is no work."""
        # Deadlines are settled before new work is claimed: a question whose
        # deadline has passed must fail its run (and escalate) even in a tenant
        # that has no other work to trigger a claim, and the sweep is a no-op
        # when nothing is overdue.
        self._service.expire_overdue_input_requests()
        claim = self._repo.claim_execution(
            worker_id=self.worker_id,
            lease_ttl_seconds=self._lease_ttl_seconds,
            reservation_ttl_seconds=self._reservation_ttl_seconds,
        )
        if claim is None:
            return None
        try:
            self._service.run_claimed_execution(claim)
        except OctopError as exc:
            # A lost fence means another worker owns the attempt now; every other
            # failure left the execution ``running`` on a lease that expires, so
            # the next claimant takes it over. Neither may stop the loop.
            logger.warning(
                "workbuddy worker %s did not commit execution %s: %s",
                self.worker_id,
                claim.execution_id,
                exc.code,
            )
        return claim.execution_id

    def drain(self, *, limit: int = 1000) -> int:
        """Run until nothing is admissible, and report how many runs that was."""
        ran = 0
        while ran < limit and self.run_once() is not None:
            ran += 1
        return ran

    async def start(self) -> None:
        """Start the loop as a background task of the current process."""
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop(self._stop))
        logger.info("workbuddy execution worker started as %s", self.worker_id)

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task is None:
            return
        task, self._task = self._task, None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - shutdown is best effort
            logger.warning("workbuddy execution worker stopped with an error: %s", exc)
        self._stop = None

    async def _loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            ran = await asyncio.to_thread(self.run_once)
            if ran is not None:
                continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._idle_sleep)
            except TimeoutError:
                continue


def _install_stop_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            loop.add_signal_handler(number, stop.set)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - platform specific
            continue


async def serve_worker(db: DatabasePool, *, worker_id: str | None = None) -> None:
    """Serve the worker loop until the process is asked to stop."""
    worker = WorkBuddyExecutionWorker(db, worker_id=worker_id)
    stop = asyncio.Event()
    _install_stop_handlers(stop)
    logger.info("workbuddy execution worker %s is serving", worker.worker_id)
    await worker.start()
    try:
        await stop.wait()
    finally:
        await worker.stop()


__all__ = [
    "DEFAULT_IDLE_SLEEP_SECONDS",
    "DEFAULT_LEASE_TTL_SECONDS",
    "WorkBuddyExecutionWorker",
    "default_worker_id",
    "serve_worker",
    "workbuddy_worker_enabled",
]
