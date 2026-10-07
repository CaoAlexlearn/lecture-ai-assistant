"""Server-owned ClassSession runtime and connection-generation lifecycle."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol


class RuntimeCatalog(Protocol):
    def get_session(self, course_id: str, lecture_id: str, session_id: str) -> dict[str, Any]: ...
    def begin_session_connection(self, course_id: str, lecture_id: str, session_id: str) -> dict[str, Any]: ...
    def set_session_runtime_state(self, course_id: str, lecture_id: str, session_id: str, state: str, **values: Any) -> dict[str, Any]: ...
    def pause_session(self, course_id: str, lecture_id: str, session_id: str, **values: Any) -> dict[str, Any]: ...
    def complete_session(self, course_id: str, lecture_id: str, session_id: str, **values: Any) -> dict[str, Any]: ...
    def resume_session(self, course_id: str, lecture_id: str, session_id: str) -> dict[str, Any]: ...


class RuntimeAgent1Pipeline(Protocol):
    async def start(self) -> None: ...
    async def checkpoint(self) -> dict[str, int]: ...
    async def shutdown(self, *, timeout_seconds: float | None) -> bool: ...
    def job_status_counts(self) -> dict[str, int]: ...
    def source_status(self) -> dict[str, int]: ...


EventSink = Callable[[dict[str, object]], Awaitable[None]]
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ConnectionLease:
    generation: int
    finished: asyncio.Event
    worker: Any | None = None
    event_sink: EventSink | None = None
    requested_action: str | None = None


class DuplicateRuntimeConnectionError(RuntimeError):
    pass


class SessionRuntimeOwner:
    """One session lifecycle owner with at most one realtime connection.

    Agent1 is optional and deliberately detached from the phase-1/2 realtime
    path.  A future Agent1 integration can consume persisted revisions without
    constraining ASR, translation, or browser state.
    """

    def __init__(
        self,
        *,
        catalog: RuntimeCatalog,
        course_id: str,
        lecture_id: str,
        session_id: str,
        agent1_pipeline: RuntimeAgent1Pipeline | None,
    ) -> None:
        self.catalog = catalog
        self.course_id = course_id
        self.lecture_id = lecture_id
        self.session_id = session_id
        self.agent1_pipeline = agent1_pipeline
        self._connection: ConnectionLease | None = None
        self._start_lock = asyncio.Lock()
        self._connection_lock = asyncio.Lock()
        self._command_lock = asyncio.Lock()
        self._started = False

    async def start(self) -> None:
        async with self._start_lock:
            if self._started:
                return
            if self.agent1_pipeline is not None:
                await self.agent1_pipeline.start()
            self._started = True

    async def attach(self) -> ConnectionLease:
        await self.start()
        async with self._connection_lock:
            if self._connection is not None and not self._connection.finished.is_set():
                raise DuplicateRuntimeConnectionError(
                    "This ClassSession already has an active realtime connection."
                )
            session = self.catalog.get_session(
                self.course_id, self.lecture_id, self.session_id
            )
            if session["status"] != "live" or session.get("runtimeState") not in {
                "disconnected",
                "interrupted",
            }:
                raise DuplicateRuntimeConnectionError(
                    "This ClassSession is not accepting a realtime connection."
                )
            session = self.catalog.begin_session_connection(
                self.course_id, self.lecture_id, self.session_id
            )
            lease = ConnectionLease(
                generation=int(session["connectionGeneration"]),
                finished=asyncio.Event(),
            )
            self._connection = lease
            return lease

    async def bind_connection(
        self, generation: int, *, worker: Any, event_sink: EventSink
    ) -> bool:
        async with self._connection_lock:
            lease = self._connection
            if lease is None or lease.generation != generation:
                return False
            lease.worker = worker
            lease.event_sink = event_sink
            should_stop = lease.requested_action is not None
        if should_stop:
            await worker.stop_when_done()
        return True

    async def emit_event(self, payload: dict[str, object]) -> None:
        if payload.get("type") == "agent1_status":
            try:
                session = self.catalog.get_session(
                    self.course_id, self.lecture_id, self.session_id
                )
                if session["status"] in {"live", "paused"}:
                    self.catalog.set_session_runtime_state(
                        self.course_id,
                        self.lecture_id,
                        self.session_id,
                        str(session.get("runtimeState", "disconnected")),
                        agent1JobStatus=self.job_status_counts(),
                        agent1SourceStatus=self.source_status(),
                    )
            except Exception:
                # Runtime metadata is observational. Durable job transitions
                # remain authoritative in the filesystem ledger.
                logger.exception("Unable to publish Agent1 runtime metadata")
        async with self._connection_lock:
            sink = self._connection.event_sink if self._connection else None
        if sink is not None:
            await sink(payload)

    async def finish_connection(self, generation: int, *, unexpected: bool) -> bool:
        """Checkpoint one generation; stale generations cannot touch current state."""

        async with self._connection_lock:
            lease = self._connection
            if lease is None or lease.generation != generation:
                return False
        checkpoint_error: Exception | None = None
        try:
            counts = (
                await self.agent1_pipeline.checkpoint()
                if self.agent1_pipeline is not None
                else {"pending": 0, "running": 0, "written": 0, "failed": 0}
            )
        except Exception as exc:  # noqa: BLE001 - clear lease after checkpoint I/O failure
            checkpoint_error = exc
            counts = self.job_status_counts()
        async with self._connection_lock:
            lease = self._connection
            if lease is None or lease.generation != generation:
                return False
            requested = lease.requested_action
            lease.event_sink = None
            lease.worker = None
            lease.finished.set()
            self._connection = None
        if unexpected and requested is None:
            self.catalog.set_session_runtime_state(
                self.course_id,
                self.lecture_id,
                self.session_id,
                "interrupted",
                agent1JobStatus=counts,
                agent1SourceStatus=self.source_status(),
                expected_generation=generation,
            )
        if checkpoint_error is not None:
            raise checkpoint_error
        return True

    async def pause(self) -> dict[str, Any]:
        async with self._command_lock:
            session = self.catalog.get_session(
                self.course_id, self.lecture_id, self.session_id
            )
            if session["status"] == "paused":
                return session
            if session["status"] != "live":
                raise ValueError("Only a live ClassSession can be paused.")
            self.catalog.set_session_runtime_state(
                self.course_id, self.lecture_id, self.session_id, "pausing"
            )
            lease = await self._request_connection_end("pause")
            if lease is not None:
                await lease.finished.wait()
            counts = await self._checkpoint()
            return self.catalog.pause_session(
                self.course_id,
                self.lecture_id,
                self.session_id,
                job_status=counts,
                source_status=self.source_status(),
            )

    async def resume(self) -> dict[str, Any]:
        async with self._command_lock:
            self.catalog.resume_session(
                self.course_id, self.lecture_id, self.session_id
            )
            return self.catalog.set_session_runtime_state(
                self.course_id,
                self.lecture_id,
                self.session_id,
                "disconnected",
            )

    async def stop(self) -> dict[str, Any]:
        async with self._command_lock:
            session = self.catalog.get_session(
                self.course_id, self.lecture_id, self.session_id
            )
            if session["status"] == "completed":
                return session
            if session["status"] not in {"live", "paused"}:
                raise ValueError("Only a live or paused ClassSession can be stopped.")
            self.catalog.set_session_runtime_state(
                self.course_id, self.lecture_id, self.session_id, "stopping"
            )
            lease = await self._request_connection_end("stop")
            if lease is not None:
                await lease.finished.wait()
            drained = True
            checkpoint_error: Exception | None = None
            try:
                await self._checkpoint()
                if self.agent1_pipeline is not None:
                    drained = await self.agent1_pipeline.shutdown(timeout_seconds=None)
            except Exception as exc:  # noqa: BLE001 - preserve recoverable Session state
                checkpoint_error = exc
                drained = False
            counts = self.job_status_counts()
            if not drained:
                self.catalog.set_session_runtime_state(
                    self.course_id,
                    self.lecture_id,
                    self.session_id,
                    "drain_failed",
                    agent1JobStatus=counts,
                    agent1SourceStatus=self.source_status(),
                )
                error = ValueError(
                    "ClassSession source/Agent1 drain did not complete; Session remains resumable."
                )
                if checkpoint_error is not None:
                    raise error from checkpoint_error
                raise error
            return self.catalog.complete_session(
                self.course_id,
                self.lecture_id,
                self.session_id,
                job_status=counts,
                notes_drained=drained,
                source_status=self.source_status(),
            )

    async def _request_connection_end(self, action: str) -> ConnectionLease | None:
        async with self._connection_lock:
            lease = self._connection
            if lease is None:
                return None
            lease.requested_action = action
            worker = lease.worker
        if worker is not None:
            await worker.stop_when_done()
        return lease

    async def _checkpoint(self) -> dict[str, int]:
        if self.agent1_pipeline is None:
            return {"pending": 0, "running": 0, "written": 0, "failed": 0}
        return await self.agent1_pipeline.checkpoint()

    def job_status_counts(self) -> dict[str, int]:
        if self.agent1_pipeline is None:
            return {"pending": 0, "running": 0, "written": 0, "failed": 0}
        return self.agent1_pipeline.job_status_counts()

    def source_status(self) -> dict[str, int]:
        if self.agent1_pipeline is None or not hasattr(
            self.agent1_pipeline, "source_status"
        ):
            return {
                "sourceCursor": 0,
                "rawSourceHead": 0,
                "bufferedSourceCount": 0,
                "durableJobCount": 0,
                "pendingJobs": 0,
                "runningJobs": 0,
                "failedJobs": 0,
            }
        return self.agent1_pipeline.source_status()

    @property
    def current_generation(self) -> int | None:
        return self._connection.generation if self._connection else None


class SessionRuntimeRegistry:
    def __init__(self) -> None:
        self._owners: dict[tuple[str, str, str], SessionRuntimeOwner] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(
        self,
        course_id: str,
        lecture_id: str,
        session_id: str,
        factory: Callable[[], SessionRuntimeOwner],
    ) -> SessionRuntimeOwner:
        key = (course_id, lecture_id, session_id)
        async with self._lock:
            owner = self._owners.get(key)
            if owner is None:
                owner = factory()
                self._owners[key] = owner
        await owner.start()
        return owner

    async def get(
        self, course_id: str, lecture_id: str, session_id: str
    ) -> SessionRuntimeOwner | None:
        async with self._lock:
            return self._owners.get((course_id, lecture_id, session_id))

    async def remove(self, course_id: str, lecture_id: str, session_id: str) -> None:
        async with self._lock:
            self._owners.pop((course_id, lecture_id, session_id), None)

    async def owners(self) -> tuple[SessionRuntimeOwner, ...]:
        async with self._lock:
            return tuple(self._owners.values())
