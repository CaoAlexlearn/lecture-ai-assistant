from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from dense_block_writer import DenseBlockWriter
from qwen_agent1 import (
    DEFAULT_PROMPT_PATH,
    QwenAgent1Config,
    QwenAgent1Error,
    QwenAgent1Worker,
)
from token_buffer import DeterministicTokenCounter, TokenBuffer
from transcript import FinalTranscript


def response(content: str, *, prompt_tokens: int = 20, output_tokens: int = 8):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=output_tokens,
            total_tokens=prompt_tokens + output_tokens,
        ),
    )


class FakeCompletions:
    def __init__(self, results: list[object]) -> None:
        self.results = results
        self.requests: list[dict[str, object]] = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeClient:
    def __init__(self, results: list[object]) -> None:
        self.completions = FakeCompletions(results)
        self.chat = SimpleNamespace(completions=self.completions)


class QwenAgent1WorkerTests(unittest.IsolatedAsyncioTestCase):
    def make_config(self, *, max_attempts: int = 2, api_key: str = "test-key"):
        return QwenAgent1Config(
            api_key=api_key,
            base_url="https://example.invalid/compatible-mode/v1",
            max_attempts=max_attempts,
        )

    @staticmethod
    def make_job():
        sentence = FinalTranscript(
            session_id="session",
            sentence_id=7,
            source_text="The lecturer defines NPV and gives an example.",
            translated_text="中文翻译哨兵",
            start_ms=100,
            end_ms=900,
        )
        buffer = TokenBuffer(
            course_id="course",
            lecture_id="lecture",
            session_id="session",
            token_counter=DeterministicTokenCounter(),
            token_target=1,
            overlap_tokens=4,
        )
        buffer.append(sentence)
        job = buffer.flush()
        assert job is not None
        return job

    async def test_request_is_source_only_and_context_tags_are_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            writer = DenseBlockWriter(Path(directory) / "dense.jsonl")
            client = FakeClient([response("The lecturer defines NPV.")])
            worker = QwenAgent1Worker(
                writer=writer, config=self.make_config(), client=client
            )
            job = replace(
                self.make_job(),
                previous_overlap_text="Prior block context only.",
            )

            await worker.process_job(job)
            writer.close()

            request = client.completions.requests[0]
            serialized = json.dumps(request, ensure_ascii=False)
            user_content = request["messages"][1]["content"]
            self.assertNotIn("中文翻译哨兵", serialized)
            self.assertIn("<PREVIOUS_CONTEXT>", user_content)
            self.assertIn("</PREVIOUS_CONTEXT>", user_content)
            self.assertIn("<CURRENT_TRANSCRIPT>", user_content)
            self.assertIn(job.current_chunk_text, user_content)
            previous_section, current_section = user_content.split(
                "<CURRENT_TRANSCRIPT>", maxsplit=1
            )
            self.assertIn("Prior block context only.", previous_section)
            self.assertNotIn("Prior block context only.", current_section)
            self.assertEqual(request["extra_body"], {"enable_thinking": False})
            self.assertEqual(request["temperature"], 0.1)
            self.assertNotIn("max_tokens", request)

    async def test_explicit_output_token_limit_is_forwarded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            writer = DenseBlockWriter(Path(directory) / "dense.jsonl")
            client = FakeClient([response("The lecturer defines NPV.")])
            worker = QwenAgent1Worker(
                writer=writer,
                config=replace(self.make_config(), max_output_tokens=8_192),
                client=client,
            )

            await worker.process_job(self.make_job())
            writer.close()

            self.assertEqual(client.completions.requests[0]["max_tokens"], 8_192)

    async def test_deepseek_provider_omits_qwen_extension_and_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            writer = DenseBlockWriter(Path(directory) / "dense.jsonl")
            client = FakeClient([response("Dense DeepSeek output.")])
            worker = QwenAgent1Worker(
                writer=writer,
                config=replace(
                    self.make_config(),
                    provider="deepseek",
                    model="deepseek-flash",
                ),
                client=client,
            )

            block = await worker.process_job(self.make_job())
            writer.close()

            self.assertNotIn("extra_body", client.completions.requests[0])
            self.assertEqual(block.provider, "deepseek")
            self.assertEqual(block.model, "deepseek-flash")

    def test_default_prompt_is_the_agent1_contract(self) -> None:
        config = self.make_config()

        self.assertEqual(config.prompt_path, DEFAULT_PROMPT_PATH)
        self.assertEqual(config.prompt_path.name, "agent1.md")
        self.assertEqual(config.prompt_path.parent.name, "agents")
        self.assertIn(
            "# Agent 1 Contract", config.prompt_path.read_text(encoding="utf-8")
        )

    async def test_empty_response_retries_then_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            writer = DenseBlockWriter(Path(directory) / "dense.jsonl")
            client = FakeClient([response("  "), response("Clean dense transcript.")])
            worker = QwenAgent1Worker(
                writer=writer, config=self.make_config(), client=client
            )

            block = await worker.process_job(self.make_job())
            writer.close()

            self.assertIsNotNone(block)
            self.assertEqual(len(client.completions.requests), 2)
            self.assertEqual(block.attempt, 2)

    async def test_qwen_output_creates_dense_block_with_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dense_path = Path(directory) / "dense.jsonl"
            writer = DenseBlockWriter(dense_path)
            client = FakeClient([response("Faithful dense output.")])
            worker = QwenAgent1Worker(
                writer=writer, config=self.make_config(), client=client
            )

            block = await worker.process_job(self.make_job())
            writer.close()

            self.assertIsNotNone(block)
            self.assertEqual(block.provider, "alibaba_cloud_model_studio")
            self.assertEqual(block.model, "qwen3.7-flash")
            self.assertIsNone(block.prompt_version)
            self.assertEqual(block.prompt_path, "agents/agent1.md")
            expected_hash = hashlib.sha256(
                DEFAULT_PROMPT_PATH.read_text(encoding="utf-8").strip().encode("utf-8")
            ).hexdigest()
            self.assertEqual(block.prompt_hash, expected_hash)
            self.assertEqual(block.input_tokens, 20)
            self.assertEqual(block.output_tokens, 8)
            record = json.loads(dense_path.read_text(encoding="utf-8"))
            self.assertEqual(record["dense_text"], "Faithful dense output.")
            self.assertEqual(record["prompt_path"], "agents/agent1.md")
            self.assertEqual(record["prompt_hash"], expected_hash)

    async def test_api_key_and_authorization_are_redacted_from_logs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            secret = "sk-test-secret-value"
            writer = DenseBlockWriter(Path(directory) / "dense.jsonl")
            client = FakeClient(
                [RuntimeError(f"request failed Authorization: Bearer {secret}")]
            )
            worker = QwenAgent1Worker(
                writer=writer,
                config=self.make_config(max_attempts=1, api_key=secret),
                client=client,
            )

            with (
                self.assertLogs("qwen_agent1", level="WARNING") as captured,
                self.assertRaises(QwenAgent1Error),
            ):
                await worker.process_job(self.make_job())
            writer.close()

            logs = "\n".join(captured.output)
            self.assertNotIn(secret, logs)
            self.assertNotIn("Bearer sk-", logs)


if __name__ == "__main__":
    unittest.main()
