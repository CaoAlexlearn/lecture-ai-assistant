"""Append-only, block-idempotent JSONL storage for Agent 1 dense blocks."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import Self, TextIO

from agent1_models import DenseBlock

logger = logging.getLogger(__name__)


def _safe_path_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return normalized or "unknown"


def scoped_dense_output_path(
    output_root: Path, *, course_id: str, lecture_id: str, session_id: str
) -> Path:
    return (
        output_root
        / _safe_path_component(course_id)
        / _safe_path_component(lecture_id)
        / _safe_path_component(session_id)
        / "lecture_dense_transcript.jsonl"
    )


class DenseBlockWriter:
    """Append DenseBlock records once, tolerating malformed existing lines."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._written_block_ids: set[str] = set()
        self._needs_line_separator = False
        self._load_existing_block_ids()
        created = not self.path.exists()
        self._file: TextIO = self.path.open("a", encoding="utf-8")
        if created:
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

    def _load_existing_block_ids(self) -> None:
        if not self.path.exists():
            return

        raw = self.path.read_bytes()
        self._needs_line_separator = bool(raw and not raw.endswith(b"\n"))
        text = raw.decode("utf-8", errors="replace")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                logger.warning(
                    "Ignoring malformed dense JSONL line %s in %s",
                    line_number,
                    self.path,
                )
                continue
            block_id = record.get("block_id") if isinstance(record, dict) else None
            if isinstance(block_id, str) and block_id:
                self._written_block_ids.add(block_id)

    def write(self, block: DenseBlock) -> bool:
        """Append a block and flush; return False when block_id already exists."""

        if block.block_id in self._written_block_ids:
            return False

        if self._needs_line_separator:
            self._file.write("\n")
            self._needs_line_separator = False

        self._file.write(
            json.dumps(asdict(block), ensure_ascii=False, separators=(",", ":"))
            + "\n"
        )
        self._file.flush()
        os.fsync(self._file.fileno())
        self._written_block_ids.add(block.block_id)
        logger.info(
            "DenseBlock written at=%s block_id=%s tokens=%s path=%s",
            block.created_at,
            block.block_id,
            block.source_token_count,
            self.path,
        )
        return True

    def last_block_sequence(self, session_id: str) -> int:
        prefix = f"{session_id}:B"
        return max(
            (
                int(block_id.removeprefix(prefix))
                for block_id in self._written_block_ids
                if block_id.startswith(prefix)
                and block_id.removeprefix(prefix).isdigit()
            ),
            default=0,
        )

    def close(self) -> None:
        if not self._file.closed:
            self._file.flush()
            self._file.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
