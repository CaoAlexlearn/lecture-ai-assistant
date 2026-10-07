from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from test_qwen_agent1 import FakeClient, response

import replay_agent1
from agent1_boundary import LastSentenceBoundaryDetector
from qwen_agent1 import QwenAgent1Config
from replay_agent1 import ReplayConfig, evaluation_paths, run_replay


class ReplayAgent1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_replay_is_offline_and_writes_only_evaluation_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_path = root / "raw.jsonl"
            records = [
                {
                    "sentence_id": 1,
                    "begin_time": 10,
                    "end_time": 20,
                    "source_text": "The teacher gives the first example.",
                    "translated_text": "中文哨兵一",
                    "sentence_end": True,
                },
                {
                    "sentence_id": 2,
                    "begin_time": 21,
                    "end_time": 30,
                    "source_text": "The example uses one hundred dollars.",
                    "translated_text": "中文哨兵二",
                    "sentence_end": True,
                },
            ]
            raw_path.write_text(
                "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
                encoding="utf-8",
            )
            output_dir = root / "evaluation" / "run"
            config = ReplayConfig(
                input_path=raw_path,
                output_dir=output_dir,
                course_id="course",
                lecture_id="lecture",
                session_id="replay-session",
                token_target=1_800,
                overlap_tokens=400,
            )
            qwen_config = QwenAgent1Config(
                api_key="test-key",
                base_url="https://example.invalid/compatible-mode/v1",
            )
            client = FakeClient([response("The teacher gives an example using $100.")])

            result = await run_replay(
                config,
                qwen_config=qwen_config,
                client=client,
                boundary_detector=LastSentenceBoundaryDetector(),
            )

            self.assertEqual(result.raw_record_count, 2)
            self.assertEqual(result.job_count, 1)
            self.assertEqual(result.dense_block_count, 1)
            self.assertEqual(result.dense_output_path.name, "dense_blocks.jsonl")
            self.assertTrue(result.comparison_path.exists())
            self.assertTrue(result.segmentation_path.exists())
            self.assertFalse((output_dir / "lecture_dense_transcript.jsonl").exists())
            request = json.dumps(
                client.completions.requests[0], ensure_ascii=False
            )
            self.assertNotIn("中文哨兵", request)

    def test_replay_module_has_no_realtime_dependencies(self) -> None:
        source = Path(replay_agent1.__file__).read_text(encoding="utf-8")
        self.assertNotIn("qwen_pipecat", source)
        self.assertNotIn("pipecat.", source)
        dense_path, comparison_path = evaluation_paths(Path("evaluation/run"))
        self.assertEqual(dense_path.name, "dense_blocks.jsonl")
        self.assertEqual(comparison_path.name, "comparison.md")


if __name__ == "__main__":
    unittest.main()
