"""Offline raw-transcript replay into the real Qwen Agent 1 worker."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import statistics
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from agent1_boundary import (
    Agent1BoundaryController,
    Agent1BoundaryDetector,
    Agent1BoundaryPolicy,
    OpenAIAgent1BoundaryDetector,
    load_boundary_detector_config,
)
from agent1_models import Agent1Job, DenseBlock
from dense_block_writer import DenseBlockWriter
from qwen_agent1 import (
    QwenAgent1Config,
    QwenAgent1Error,
    QwenAgent1Worker,
    load_qwen_config,
)
from token_buffer import DeterministicTokenCounter, TokenBuffer
from transcript import FinalTranscript

logger = logging.getLogger(__name__)


class ReplayError(RuntimeError):
    """Raised for invalid raw input or an incomplete offline replay."""


@dataclass(frozen=True, slots=True)
class ReplayConfig:
    input_path: Path
    output_dir: Path
    course_id: str
    lecture_id: str
    session_id: str
    start_line: int = 1
    end_line: int | None = None
    boundary_check_start_tokens: int = 900
    min_block_tokens: int = 450
    lookahead_reserve_tokens: int = 180
    recheck_growth_tokens: int = 200
    max_tokens: int = 1_800
    overlap_tokens: int = 120
    # Read-only compatibility for older callers. New replay uses boundary policy.
    token_target: int | None = None


@dataclass(frozen=True, slots=True)
class ReplayResult:
    raw_record_count: int
    job_count: int
    dense_block_count: int
    average_latency_ms: float
    dense_output_path: Path
    comparison_path: Path
    segmentation_path: Path


def evaluation_paths(output_dir: Path) -> tuple[Path, Path]:
    return output_dir / "dense_blocks.jsonl", output_dir / "comparison.md"


def segmentation_evaluation_path(output_dir: Path) -> Path:
    return output_dir / "segmentation.json"


def read_raw_sentences(
    path: Path, *, start_line: int = 1, end_line: int | None = None
) -> list[FinalTranscript]:
    if start_line < 1:
        raise ReplayError("--start-line must be at least 1.")
    if end_line is not None and end_line < start_line:
        raise ReplayError("--end-line must be greater than or equal to --start-line.")

    sentences: list[FinalTranscript] = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if line_number < start_line:
                continue
            if end_line is not None and line_number > end_line:
                break
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ReplayError(
                    f"Malformed JSON at input line {line_number}."
                ) from exc
            if record.get("sentence_end") is not True:
                continue
            source_text = str(record.get("source_text") or "")
            translated_text = str(record.get("translated_text") or "")
            if not source_text:
                continue
            sentences.append(
                FinalTranscript(
                    session_id=str(record.get("session_id") or "replay"),
                    sentence_id=record.get(
                        "source_identity", record.get("sentence_id", line_number)
                    ),
                    source_text=source_text,
                    translated_text=translated_text,
                    start_ms=_optional_int(record.get("begin_time")),
                    end_ms=_optional_int(record.get("end_time")),
                )
            )
    return sentences


async def build_replay_jobs(
    sentences: list[FinalTranscript],
    config: ReplayConfig,
    *,
    boundary_detector: Agent1BoundaryDetector,
) -> list[Agent1Job]:
    """Apply the same rolling controller and prefix-consumption policy as production."""

    counter = DeterministicTokenCounter()
    buffer = TokenBuffer(
        course_id=config.course_id,
        lecture_id=config.lecture_id,
        session_id=config.session_id,
        token_counter=counter,
        max_tokens=config.max_tokens,
        time_trigger_min_tokens=min(100, config.max_tokens),
        overlap_tokens=config.overlap_tokens,
    )
    controller = Agent1BoundaryController(
        detector=boundary_detector,
        policy=Agent1BoundaryPolicy(
            check_start_tokens=config.boundary_check_start_tokens,
            min_block_tokens=config.min_block_tokens,
            lookahead_reserve_tokens=config.lookahead_reserve_tokens,
            recheck_growth_tokens=config.recheck_growth_tokens,
            max_tokens=config.max_tokens,
        ),
        token_counter=counter,
    )
    jobs: list[Agent1Job] = []
    for sentence in sentences:
        buffer.append(sentence)
        while True:
            snapshot = controller.snapshot(buffer.snapshot_sentences())
            if not controller.should_check(snapshot):
                break
            controller.begin_check()
            decision = None
            error: str | None = None
            try:
                decision = await boundary_detector.detect(
                    snapshot,
                    hard_max_reached=snapshot.token_count >= config.max_tokens,
                )
            except Exception as exc:  # noqa: BLE001 - hard max uses shared fallback
                error = f"{type(exc).__name__}: {exc}"
            resolution = controller.resolve(
                snapshot,
                buffer.snapshot_sentences(),
                decision,
                detector_error=error,
            )
            if resolution is None:
                controller.finish_wait(snapshot)
                break
            jobs.append(
                buffer.consume_prefix(
                    resolution.cut_after_source_id,
                    boundary_metadata=resolution.metadata,
                )
            )
            controller.finish_cut()
    snapshot = controller.snapshot(buffer.snapshot_sentences())
    tail = buffer.flush(
        boundary_metadata=(
            controller.final_tail_metadata(snapshot) if snapshot.sentences else None
        )
    )
    if tail is not None:
        jobs.append(tail)
    return jobs


async def run_replay(
    config: ReplayConfig,
    *,
    qwen_config: QwenAgent1Config | None = None,
    client: Any | None = None,
    boundary_detector: Agent1BoundaryDetector | None = None,
) -> ReplayResult:
    sentences = read_raw_sentences(
        config.input_path,
        start_line=config.start_line,
        end_line=config.end_line,
    )
    if not sentences:
        raise ReplayError("No final source sentences were found in the selected range.")
    detector = boundary_detector or OpenAIAgent1BoundaryDetector(
        load_boundary_detector_config()
    )
    jobs = await build_replay_jobs(sentences, config, boundary_detector=detector)

    dense_path, comparison_path = evaluation_paths(config.output_dir)
    segmentation_path = segmentation_evaluation_path(config.output_dir)
    if dense_path.name == "lecture_dense_transcript.jsonl":
        raise ReplayError(
            "Evaluation output cannot target the production dense filename."
        )
    resolved_qwen_config = qwen_config or load_qwen_config()
    write_segmentation_evaluation(
        segmentation_path,
        sentences,
        jobs,
        config=config,
        boundary_detector=detector,
        agent1_config=resolved_qwen_config,
        dense_written_jobs=0,
        failed_jobs=0,
    )
    writer = DenseBlockWriter(dense_path)
    worker = QwenAgent1Worker(
        writer=writer,
        config=resolved_qwen_config,
        client=client,
    )
    queue: asyncio.Queue[Agent1Job | None] = asyncio.Queue()
    worker_task = asyncio.create_task(worker.run(queue), name="qwen-agent1-replay")
    try:
        for job in jobs:
            queue.put_nowait(job)
        queue.put_nowait(None)
        await queue.join()
        await worker_task
    finally:
        if not worker_task.done():
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
        writer.close()

    write_segmentation_evaluation(
        segmentation_path,
        sentences,
        jobs,
        config=config,
        boundary_detector=detector,
        agent1_config=resolved_qwen_config,
        dense_written_jobs=len(worker.completed_blocks),
        failed_jobs=worker.failed,
    )
    if worker.failed:
        raise ReplayError(f"{worker.failed} Agent 1 job(s) failed.")
    if len(worker.completed_blocks) != len(jobs):
        raise ReplayError(
            "Evaluation output was deduplicated or incomplete; use a new output directory."
        )

    write_comparison(comparison_path, jobs, worker.completed_blocks)
    latencies = [block.latency_ms for block in worker.completed_blocks]
    return ReplayResult(
        raw_record_count=len(sentences),
        job_count=len(jobs),
        dense_block_count=len(worker.completed_blocks),
        average_latency_ms=sum(latencies) / len(latencies),
        dense_output_path=dense_path,
        comparison_path=comparison_path,
        segmentation_path=segmentation_path,
    )


def write_segmentation_evaluation(
    path: Path,
    sentences: list[FinalTranscript],
    jobs: list[Agent1Job],
    *,
    config: ReplayConfig,
    boundary_detector: Agent1BoundaryDetector,
    agent1_config: QwenAgent1Config,
    dense_written_jobs: int,
    failed_jobs: int,
) -> None:
    actual_ids = [source_id for job in jobs for source_id in job.source_sentence_ids]
    expected_ids = [sentence.sentence_id for sentence in sentences]
    token_counts = [job.current_chunk_token_count for job in jobs]
    fallback_count = sum(
        1
        for job in jobs
        if job.boundary_metadata is not None and job.boundary_metadata.boundary_fallback
    )
    type_distribution: dict[str, int] = {}
    for job in jobs:
        boundary_type = (
            job.boundary_metadata.boundary_type
            if job.boundary_metadata is not None
            else None
        )
        key = boundary_type or "final_tail"
        type_distribution[key] = type_distribution.get(key, 0) + 1
    source_positions = {
        sentence.sentence_id: index for index, sentence in enumerate(sentences)
    }
    total_checks = sum(
        job.boundary_metadata.boundary_check_count
        for job in jobs
        if job.boundary_metadata is not None
    )
    semantic_cut_count = sum(
        1
        for job in jobs
        if job.boundary_metadata is not None
        and job.boundary_metadata.boundary_decision == "cut"
    )
    wait_count = max(0, total_checks - semantic_cut_count)
    payload = {
        "sourcePartitionValid": actual_ids == expected_ids,
        "sourceFinalCount": len(sentences),
        "blockCount": len(jobs),
        "minBlockTokens": min(token_counts, default=0),
        "maxBlockTokens": max(token_counts, default=0),
        "averageBlockTokens": (
            sum(token_counts) / len(token_counts) if token_counts else 0
        ),
        "medianBlockTokens": statistics.median(token_counts) if token_counts else 0,
        "boundaryTypeDistribution": type_distribution,
        "waitCount": wait_count,
        "averageChecksPerEmittedBlock": (total_checks / len(jobs) if jobs else 0),
        "fallbackCount": fallback_count,
        "denseWrittenJobs": dense_written_jobs,
        "failedJobs": failed_jobs,
        "configuration": {
            "boundaryCheckStartTokens": config.boundary_check_start_tokens,
            "minBlockTokens": config.min_block_tokens,
            "lookaheadReserveTokens": config.lookahead_reserve_tokens,
            "recheckGrowthTokens": config.recheck_growth_tokens,
            "hardMaxTokens": config.max_tokens,
            "overlapTokens": config.overlap_tokens,
            "boundaryProvider": boundary_detector.provider,
            "boundaryModel": boundary_detector.model,
            "agent1Provider": agent1_config.provider,
            "agent1Model": agent1_config.model,
            "agent1PromptPath": str(agent1_config.prompt_path),
        },
        "blocks": [
            {
                "blockId": job.block_id,
                "sourceSentenceIds": list(job.source_sentence_ids),
                "sourceRange": [
                    job.source_sentence_ids[0],
                    job.source_sentence_ids[-1],
                ],
                "previousContext": job.previous_overlap_text,
                "currentTranscript": job.current_chunk_text,
                "sourceTokenCount": job.current_chunk_token_count,
                "boundarySentence": job.current_chunk_text.splitlines()[-1],
                "contextBefore": [
                    sentences[index].source_text
                    for index in range(
                        max(0, source_positions[job.source_sentence_ids[-1]] - 4),
                        source_positions[job.source_sentence_ids[-1]],
                    )
                ],
                "contextAfter": [
                    sentences[index].source_text
                    for index in range(
                        source_positions[job.source_sentence_ids[-1]] + 1,
                        min(
                            len(sentences),
                            source_positions[job.source_sentence_ids[-1]] + 5,
                        ),
                    )
                ],
                "boundary": (
                    None
                    if job.boundary_metadata is None
                    else {
                        "decision": job.boundary_metadata.boundary_decision,
                        "type": job.boundary_metadata.boundary_type,
                        "provider": (job.boundary_metadata.boundary_detector_provider),
                        "model": job.boundary_metadata.boundary_detector_model,
                        "checkCount": job.boundary_metadata.boundary_check_count,
                        "bufferTokensAtCheck": (
                            job.boundary_metadata.buffer_tokens_at_check
                        ),
                        "prefixTokens": job.boundary_metadata.prefix_tokens,
                        "suffixTokens": job.boundary_metadata.suffix_tokens,
                        "fallback": job.boundary_metadata.boundary_fallback,
                        "snapshotSourceSequenceStart": (
                            job.boundary_metadata.snapshot_source_sequence_start
                        ),
                        "snapshotSourceSequenceEnd": (
                            job.boundary_metadata.snapshot_source_sequence_end
                        ),
                        "cutSourceSequence": (
                            job.boundary_metadata.cut_source_sequence
                        ),
                    }
                ),
            }
            for job in jobs
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def write_comparison(
    path: Path, jobs: list[Agent1Job], blocks: list[DenseBlock]
) -> None:
    counter = DeterministicTokenCounter()
    blocks_by_id = {block.block_id: block for block in blocks}
    sections = ["# Agent 1 Faithful Compression Evaluation", ""]
    for job in jobs:
        block = blocks_by_id[job.block_id]
        output_token_count = counter.count(block.dense_text)
        ratio = output_token_count / job.current_chunk_token_count
        sections.extend(
            [
                f"## {job.block_id}",
                "",
                f"- Latency: {block.latency_ms:.3f} ms",
                f"- API input tokens: {_display_usage(block.input_tokens)}",
                f"- API output tokens: {_display_usage(block.output_tokens)}",
                f"- Compression ratio: {ratio:.3f}",
                "",
                "### RAW CURRENT TRANSCRIPT",
                "",
                "````text",
                job.current_chunk_text,
                "````",
                "",
                "### QWEN DENSE TRANSCRIPT",
                "",
                "````text",
                block.dense_text,
                "````",
                "",
            ]
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(sections), encoding="utf-8")


def _display_usage(value: int | None) -> str:
    return str(value) if value is not None else "n/a"


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _default_output_dir() -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path("evaluation") / f"agent1-{timestamp}-{uuid.uuid4().hex[:8]}"


def parse_args() -> argparse.Namespace:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--start-line", type=int, default=1)
    parser.add_argument("--end-line", type=int)
    parser.add_argument("--output-dir", type=Path, default=_default_output_dir())
    parser.add_argument("--course-id", default=os.getenv("COURSE_ID", "replay-course"))
    parser.add_argument(
        "--lecture-id", default=os.getenv("LECTURE_ID", "replay-lecture")
    )
    parser.add_argument("--session-id", default=f"replay-{uuid.uuid4().hex}")
    parser.add_argument(
        "--boundary-check-start-tokens",
        type=int,
        default=int(os.getenv("AGENT1_BOUNDARY_CHECK_START_TOKENS", "900")),
    )
    parser.add_argument(
        "--min-block-tokens",
        type=int,
        default=int(os.getenv("AGENT1_MIN_BLOCK_TOKENS", "450")),
    )
    parser.add_argument(
        "--lookahead-reserve-tokens",
        type=int,
        default=int(os.getenv("AGENT1_LOOKAHEAD_RESERVE_TOKENS", "180")),
    )
    parser.add_argument(
        "--recheck-growth-tokens",
        type=int,
        default=int(os.getenv("AGENT1_BOUNDARY_RECHECK_GROWTH_TOKENS", "200")),
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=int(os.getenv("AGENT1_MAX_TOKENS", "1800")),
    )
    parser.add_argument(
        "--overlap-tokens",
        type=int,
        default=int(os.getenv("AGENT1_OVERLAP_TOKENS", "120")),
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = parse_args()
    config = ReplayConfig(
        input_path=args.input,
        output_dir=args.output_dir,
        course_id=args.course_id,
        lecture_id=args.lecture_id,
        session_id=args.session_id,
        start_line=args.start_line,
        end_line=args.end_line,
        boundary_check_start_tokens=args.boundary_check_start_tokens,
        min_block_tokens=args.min_block_tokens,
        lookahead_reserve_tokens=args.lookahead_reserve_tokens,
        recheck_growth_tokens=args.recheck_growth_tokens,
        max_tokens=args.max_tokens,
        overlap_tokens=args.overlap_tokens,
    )
    try:
        result = asyncio.run(run_replay(config))
    except (OSError, ReplayError, QwenAgent1Error) as exc:
        logger.error("Offline replay failed: %s", exc)
        return 1
    print(
        f"Replay complete: raw_records={result.raw_record_count} "
        f"jobs={result.job_count} dense_blocks={result.dense_block_count} "
        f"average_latency_ms={result.average_latency_ms:.3f}"
    )
    print(f"Dense output: {result.dense_output_path}")
    print(f"Comparison: {result.comparison_path}")
    print(f"Segmentation: {result.segmentation_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
