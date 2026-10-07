from __future__ import annotations

import json
import unittest
from dataclasses import asdict, fields

from agent1_models import Agent1Job


class Agent1ModelTests(unittest.TestCase):
    def test_agent1_job_has_no_translation_field_or_chinese_payload(self) -> None:
        job = Agent1Job(
            course_id="course",
            lecture_id="lecture",
            session_id="session",
            block_id="session:B000001",
            source_sentence_ids=(1,),
            previous_overlap_text="previous English context",
            current_chunk_text="current English source",
            start_time=100,
            end_time=200,
            current_chunk_token_count=3,
        )

        field_names = {field.name for field in fields(Agent1Job)}
        serialized = json.dumps(asdict(job), ensure_ascii=False)
        self.assertNotIn("translated_text", field_names)
        self.assertNotIn("中文哨兵", serialized)
        self.assertEqual(job.current_chunk_text, "current English source")


if __name__ == "__main__":
    unittest.main()
