from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent1_models import DenseBlock
from dense_block_writer import DenseBlockWriter


def dense_block(block_id: str = "session:B000001") -> DenseBlock:
    return DenseBlock(
        course_id="course",
        lecture_id="lecture",
        session_id="session",
        block_id=block_id,
        source_sentence_ids=(1, 2),
        start_time=100,
        end_time=300,
        source_token_count=20,
        dense_text="FAKE_AGENT1_OUTPUT",
        created_at="2026-09-08T00:00:00+00:00",
        attempt=1,
    )


class DenseBlockWriterTests(unittest.TestCase):
    def test_same_block_id_is_appended_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dense.jsonl"
            writer = DenseBlockWriter(path)
            self.assertTrue(writer.write(dense_block()))
            self.assertFalse(writer.write(dense_block()))
            writer.close()

            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)

    def test_existing_block_id_is_loaded_for_retry_deduplication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dense.jsonl"
            first_writer = DenseBlockWriter(path)
            first_writer.write(dense_block())
            first_writer.close()

            restarted_writer = DenseBlockWriter(path)
            self.assertFalse(restarted_writer.write(dense_block()))
            self.assertEqual(restarted_writer.last_block_sequence("session"), 1)
            restarted_writer.close()
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)

    def test_malformed_trailing_line_does_not_prevent_start_or_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dense.jsonl"
            path.write_text('{"block_id":"partial', encoding="utf-8")

            with self.assertLogs("dense_block_writer", level="WARNING"):
                writer = DenseBlockWriter(path)
            self.assertTrue(writer.write(dense_block()))
            writer.close()

            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], '{"block_id":"partial')
            self.assertEqual(json.loads(lines[-1])["block_id"], "session:B000001")


if __name__ == "__main__":
    unittest.main()
