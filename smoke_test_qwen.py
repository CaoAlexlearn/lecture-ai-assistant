"""Minimal live Qwen3.7-Flash Agent 1 smoke test without microphone input."""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from agent1_models import Agent1Job
from dense_block_writer import DenseBlockWriter
from qwen_agent1 import QwenAgent1Error, QwenAgent1Worker, load_qwen_config


async def async_main() -> None:
    session_id = f"smoke-{uuid.uuid4().hex}"
    output_path = Path("evaluation") / session_id / "dense_blocks.jsonl"
    writer = DenseBlockWriter(output_path)
    worker = QwenAgent1Worker(writer=writer, config=load_qwen_config())
    job = Agent1Job(
        course_id="phase3a-smoke",
        lecture_id="qwen-api",
        session_id=session_id,
        block_id=f"{session_id}:B000001",
        source_sentence_ids=("smoke-1",),
        previous_overlap_text="",
        current_chunk_text=(
            "The lecturer says that net present value compares discounted future "
            "cash flows with the initial investment."
        ),
        start_time=None,
        end_time=None,
        current_chunk_token_count=17,
    )
    try:
        block = await worker.process_job(job)
    finally:
        writer.close()
    if block is None:
        raise QwenAgent1Error("Smoke block was unexpectedly deduplicated.")
    print(
        f"Smoke test passed: model={block.model} latency_ms={block.latency_ms:.3f} "
        f"input_tokens={block.input_tokens} output_tokens={block.output_tokens}"
    )
    print(f"Output: {output_path}")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(async_main())
    except (OSError, QwenAgent1Error) as exc:
        logging.getLogger(__name__).error("Qwen smoke test failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
