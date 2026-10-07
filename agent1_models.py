"""Source-only data models for the asynchronous Agent 1 pipeline."""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Protocol

SentenceId = int | str


@dataclass(frozen=True, slots=True)
class Agent1BoundaryMetadata:
    boundary_decision: str
    boundary_type: str | None
    boundary_detector_provider: str
    boundary_detector_model: str
    boundary_check_count: int
    buffer_tokens_at_check: int
    prefix_tokens: int
    suffix_tokens: int
    boundary_fallback: bool
    snapshot_source_sequence_start: int | None
    snapshot_source_sequence_end: int | None
    cut_source_sequence: int | None


@dataclass(frozen=True, slots=True)
class Agent1Job:
    course_id: str
    lecture_id: str
    session_id: str
    block_id: str
    source_sentence_ids: tuple[SentenceId, ...]
    previous_overlap_text: str
    current_chunk_text: str
    start_time: int | None
    end_time: int | None
    current_chunk_token_count: int
    source_sequence_start: int | None = None
    source_sequence_end: int | None = None
    boundary_metadata: Agent1BoundaryMetadata | None = None


@dataclass(frozen=True, slots=True)
class DenseBlock:
    course_id: str
    lecture_id: str
    session_id: str
    block_id: str
    source_sentence_ids: tuple[SentenceId, ...]
    start_time: int | None
    end_time: int | None
    source_token_count: int
    dense_text: str
    created_at: str
    worker_type: str = "fake"
    attempt: int = 1
    provider: str = "local"
    model: str = "fake-agent1"
    prompt_version: str | None = None
    prompt_path: str | None = None
    prompt_hash: str | None = None
    latency_ms: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class WorkerLifecycleHook(Protocol):
    def __call__(
        self,
        event: str,
        job: Agent1Job,
        block: DenseBlock | None,
        attempt: int,
    ) -> Awaitable[None] | None: ...
