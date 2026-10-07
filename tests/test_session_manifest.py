from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from session_manifest import SessionManifestWriter


class SessionManifestWriterTests(unittest.TestCase):
    def test_manifest_records_run_provenance_and_final_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "course" / "lecture" / "session" / "session_manifest.json"
            raw_path = root / "raw.jsonl"
            dense_path = path.with_name("lecture_dense_transcript.jsonl")

            manifest = SessionManifestWriter(
                path,
                course_id="course",
                lecture_id="lecture",
                session_id="session",
                raw_output_path=raw_path,
                dense_output_path=dense_path,
                agent1_mode="fake",
            )

            started = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsNone(started["ended_at"])
            self.assertEqual(started["agent1_mode"], "fake")

            manifest.finish(pipeline_metrics={"dense_block_count": 3})
            finished = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(finished["course_id"], "course")
            self.assertEqual(finished["lecture_id"], "lecture")
            self.assertEqual(finished["session_id"], "session")
            self.assertIsNotNone(finished["started_at"])
            self.assertIsNotNone(finished["ended_at"])
            self.assertEqual(finished["raw_output_path"], str(raw_path.resolve()))
            self.assertEqual(finished["dense_output_path"], str(dense_path.resolve()))
            self.assertEqual(
                finished["pipeline_metrics"]["dense_block_count"], 3
            )

            resumed = SessionManifestWriter(
                path,
                course_id="course",
                lecture_id="lecture",
                session_id="session",
                raw_output_path=raw_path,
                dense_output_path=dense_path,
                agent1_mode="fake",
                connection_generation=2,
            )
            resumed_record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(resumed_record["started_at"], finished["started_at"])
            self.assertEqual(resumed_record["connection_count"], 2)
            resumed.finish()
            final_record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                [item["generation"] for item in final_record["connections"]],
                [1, 2],
            )
            self.assertTrue(
                all(item["ended_at"] for item in final_record["connections"])
            )


if __name__ == "__main__":
    unittest.main()
