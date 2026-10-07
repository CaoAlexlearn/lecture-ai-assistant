"""Crash-recoverable filesystem ledger for Agent1 transcript jobs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent1_models import Agent1BoundaryMetadata, Agent1Job

_BLOCK_SEQUENCE = re.compile(r":B(\d+)$")
_SETTLED_STATUSES = frozenset({"written", "failed"})


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class Agent1JobLedger:
    """Persist every Agent1 job before it becomes eligible for execution."""

    def __init__(
        self,
        root: Path,
        *,
        session_id: str,
        prompt_version_id: str | None,
        instruction_snapshot: Mapping[str, Any] | None,
    ) -> None:
        self.root = root
        self.jobs_root = root / "jobs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id
        self.prompt_version_id = prompt_version_id
        self.instruction_snapshot_hash = _canonical_hash(instruction_snapshot or {})
        self._lock = threading.RLock()

    def create_pending(self, job: Agent1Job) -> dict[str, Any]:
        """Create an immutable job payload with pending execution state."""

        with self._lock:
            path = self._path(job.block_id)
            if path.is_file():
                existing = self._read(path)
                if _canonical_hash(existing.get("job")) != _canonical_hash(asdict(job)):
                    raise ValueError(
                        f"Agent1 job ID collision has different payload: {job.block_id}"
                    )
                return existing
            now = _now()
            payload = asdict(job)
            record: dict[str, Any] = {
                "schemaVersion": 1,
                "jobId": job.block_id,
                "sessionId": job.session_id,
                "chunkSequence": self._sequence(job.block_id),
                "sourceSentenceIds": list(job.source_sentence_ids),
                "sourceSequenceRange": [
                    job.source_sequence_start,
                    job.source_sequence_end,
                ],
                "currentTranscript": job.current_chunk_text,
                "previousContext": job.previous_overlap_text,
                "promptVersionId": self.prompt_version_id,
                "instructionSnapshotHash": self.instruction_snapshot_hash,
                "status": "pending",
                "attempt": 0,
                "createdAt": now,
                "updatedAt": now,
                "job": payload,
                "error": None,
            }
            self._write(path, record)
            return record

    def recover_interrupted(self) -> int:
        """Return abandoned running work to pending after process restart."""

        recovered = 0
        with self._lock:
            for path in self._paths():
                record = self._read(path)
                if record.get("status") != "running":
                    continue
                record["status"] = "pending"
                record["error"] = "Worker was interrupted before completion."
                record["updatedAt"] = _now()
                self._write(path, record)
                recovered += 1
        return recovered

    def mark_running(self, job_id: str, attempt: int) -> None:
        self._transition(job_id, "running", attempt=attempt, error=None)

    def mark_written(self, job_id: str, attempt: int) -> None:
        self._transition(job_id, "written", attempt=attempt, error=None)

    def mark_failed(self, job_id: str, attempt: int, error: str) -> None:
        self._transition(job_id, "failed", attempt=attempt, error=error[:1000])

    def pending_jobs(self) -> list[Agent1Job]:
        with self._lock:
            records = []
            for path in self._paths():
                record = self._read(path)
                if record.get("status") == "pending":
                    records.append(record)
        return [self._job(record) for record in records]

    def jobs(self) -> list[Agent1Job]:
        """Return every durable job in block order, regardless of worker status."""

        with self._lock:
            return [self._job(self._read(path)) for path in self._paths()]

    def job_for_sequence(self, sequence: int) -> Agent1Job | None:
        """Return one already committed segmentation decision, if present."""

        if sequence <= 0:
            return None
        path = self.jobs_root / f"J{sequence:06d}.json"
        with self._lock:
            return self._job(self._read(path)) if path.is_file() else None

    def source_partition_report(
        self, expected_source_ids: list[int | str]
    ) -> dict[str, Any]:
        """Validate ordered, exactly-once ownership independently of execution."""

        actual = [
            source_id for job in self.jobs() for source_id in job.source_sentence_ids
        ]
        positions: dict[int | str, list[int]] = {}
        for index, source_id in enumerate(actual):
            positions.setdefault(source_id, []).append(index)
        expected_set = set(expected_source_ids)
        actual_set = set(actual)
        duplicates = {
            str(source_id): indexes
            for source_id, indexes in positions.items()
            if len(indexes) > 1
        }
        return {
            "valid": actual == expected_source_ids,
            "expectedCount": len(expected_source_ids),
            "ownedCount": len(actual),
            "missingSourceIds": [
                source_id
                for source_id in expected_source_ids
                if source_id not in actual_set
            ],
            "unexpectedSourceIds": [
                source_id for source_id in actual if source_id not in expected_set
            ],
            "duplicateSourceIds": duplicates,
            "missingCount": sum(
                1 for source_id in expected_source_ids if source_id not in actual_set
            ),
            "duplicateCount": sum(len(indexes) - 1 for indexes in duplicates.values()),
            "overlapCount": sum(len(indexes) - 1 for indexes in duplicates.values()),
            "outOfOrderCount": sum(
                1
                for index, source_id in enumerate(actual[: len(expected_source_ids)])
                if source_id != expected_source_ids[index]
            ),
            "ordered": actual == expected_source_ids,
        }

    def status_counts(self) -> dict[str, int]:
        counts = {"pending": 0, "running": 0, "written": 0, "failed": 0}
        with self._lock:
            for path in self._paths():
                status = str(self._read(path).get("status"))
                if status in counts:
                    counts[status] += 1
        return counts

    def has_unsettled(self) -> bool:
        counts = self.status_counts()
        return bool(counts["pending"] or counts["running"])

    def last_sequence(self) -> int:
        with self._lock:
            return max(
                (
                    self._sequence(str(self._read(path).get("jobId", "")))
                    for path in self._paths()
                ),
                default=0,
            )

    def source_recovery_baseline(self) -> dict[str, Any] | None:
        """Describe the last durable job for checkpoint bootstrap/migration."""

        with self._lock:
            records = [self._read(path) for path in self._paths()]
        if not records:
            return None
        last = max(records, key=lambda item: int(item.get("chunkSequence", 0)))
        job = self._job(last)
        return {
            "chunkSequence": int(last["chunkSequence"]),
            "lastConsumedSourceSequence": job.source_sequence_end,
            "currentTranscript": job.current_chunk_text,
        }

    def settled_source_ids(self) -> set[int | str]:
        """Return source identities owned by written or explicitly failed jobs."""

        owned: set[int | str] = set()
        with self._lock:
            for path in self._paths():
                record = self._read(path)
                if record.get("status") not in _SETTLED_STATUSES:
                    continue
                owned.update(self._job(record).source_sentence_ids)
        return owned

    def _transition(
        self, job_id: str, status: str, *, attempt: int, error: str | None
    ) -> None:
        with self._lock:
            path = self._path(job_id)
            record = self._read(path)
            if record.get("status") in _SETTLED_STATUSES and status == "running":
                return
            record["status"] = status
            record["attempt"] = attempt
            record["error"] = error
            record["updatedAt"] = _now()
            self._write(path, record)

    def _path(self, job_id: str) -> Path:
        sequence = self._sequence(job_id)
        if sequence <= 0 or not job_id.startswith(f"{self.session_id}:B"):
            raise ValueError("Agent1 job ID does not belong to this session")
        return self.jobs_root / f"J{sequence:06d}.json"

    def _paths(self) -> list[Path]:
        return sorted(self.jobs_root.glob("J*.json"))

    @staticmethod
    def _sequence(job_id: str) -> int:
        match = _BLOCK_SEQUENCE.search(job_id)
        return int(match.group(1)) if match else 0

    @staticmethod
    def _job(record: Mapping[str, Any]) -> Agent1Job:
        value = dict(record["job"])
        value["source_sentence_ids"] = tuple(value["source_sentence_ids"])
        metadata = value.get("boundary_metadata")
        if isinstance(metadata, dict):
            value["boundary_metadata"] = Agent1BoundaryMetadata(**metadata)
        return Agent1Job(**value)

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise TypeError(f"Invalid Agent1 job ledger record: {path.name}")
        return value

    @staticmethod
    def _write(path: Path, record: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(
                    (
                        json.dumps(dict(record), ensure_ascii=False, indent=2) + "\n"
                    ).encode("utf-8")
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
