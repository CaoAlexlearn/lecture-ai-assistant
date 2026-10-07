from __future__ import annotations

import base64
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from deepseek_client import (
    DEFAULT_DEEPSEEK_BASE_URL,
    DEFAULT_DEEPSEEK_MODEL,
    DEFAULT_DEEPSEEK_PRO_MODEL,
    DEFAULT_DEEPSEEK_VISION_MODEL,
    DeepSeekClient,
    DeepSeekConfig,
    DeepSeekConfigurationError,
    DeepSeekResponseError,
    build_vision_user_message,
    load_deepseek_config,
)
from workspace_tools import ImageReadResult


class FakeCompletions:
    def __init__(self, response: object | None = None) -> None:
        self.requests: list[dict[str, object]] = []
        self.response = response or SimpleNamespace()

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.response


class FakeClient:
    def __init__(self, response: object | None = None) -> None:
        self.completions = FakeCompletions(response)
        self.chat = SimpleNamespace(completions=self.completions)


def test_image() -> ImageReadResult:
    return ImageReadResult(
        path="source/mineru/images/slide.png",
        data=b"\x89PNG\r\n\x1a\nexample",
        format="png",
        mime_type="image/png",
        size_bytes=15,
    )


class DeepSeekConfigTests(unittest.TestCase):
    @mock.patch("deepseek_client.load_dotenv")
    @mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "secret"}, clear=True)
    def test_loads_documented_defaults(self, _load_dotenv: mock.Mock) -> None:
        config = load_deepseek_config()

        self.assertEqual(config.base_url, DEFAULT_DEEPSEEK_BASE_URL)
        self.assertEqual(config.model, DEFAULT_DEEPSEEK_MODEL)
        self.assertEqual(config.pro_model, DEFAULT_DEEPSEEK_PRO_MODEL)
        self.assertEqual(config.vision_model, DEFAULT_DEEPSEEK_VISION_MODEL)
        self.assertEqual(config.timeout_seconds, 90.0)
        self.assertEqual(config.max_retries, 2)
        self.assertNotIn("secret", repr(config))

    def test_all_default_model_roles_use_deepseek_flash(self) -> None:
        config = DeepSeekConfig(api_key="secret")

        self.assertEqual(
            (
                DEFAULT_DEEPSEEK_MODEL,
                DEFAULT_DEEPSEEK_PRO_MODEL,
                DEFAULT_DEEPSEEK_VISION_MODEL,
            ),
            ("deepseek-flash", "deepseek-flash", "deepseek-flash"),
        )
        self.assertEqual(
            (config.model, config.pro_model, config.vision_model),
            ("deepseek-flash", "deepseek-flash", "deepseek-flash"),
        )

    @mock.patch("deepseek_client.load_dotenv")
    @mock.patch.dict(
        os.environ,
        {
            "DEEPSEEK_API_KEY": "secret",
            "DEEPSEEK_MODEL_PRO": "custom-pro-model",
        },
        clear=True,
    )
    def test_loads_configured_pro_model(self, _load_dotenv: mock.Mock) -> None:
        self.assertEqual(load_deepseek_config().pro_model, "custom-pro-model")

    @mock.patch("deepseek_client.load_dotenv")
    @mock.patch.dict(os.environ, {}, clear=True)
    def test_api_key_is_required(self, _load_dotenv: mock.Mock) -> None:
        with self.assertRaisesRegex(
            DeepSeekConfigurationError, "DEEPSEEK_API_KEY is missing"
        ):
            load_deepseek_config()

    @mock.patch("deepseek_client.load_dotenv")
    @mock.patch.dict(
        os.environ,
        {"DEEPSEEK_API_KEY": "secret", "DEEPSEEK_MAX_RETRIES": "-1"},
        clear=True,
    )
    def test_invalid_numeric_configuration_is_rejected(
        self, _load_dotenv: mock.Mock
    ) -> None:
        with self.assertRaisesRegex(DeepSeekConfigurationError, "cannot be negative"):
            load_deepseek_config()


class DeepSeekClientTests(unittest.IsolatedAsyncioTestCase):
    def make_client(
        self, fake: FakeClient | None = None
    ) -> tuple[DeepSeekClient, FakeClient]:
        fake = fake or FakeClient()
        adapter = DeepSeekClient(DeepSeekConfig(api_key="secret"), client=fake)
        return adapter, fake

    async def test_text_completion_omits_optional_generation_limits(self) -> None:
        adapter, fake = self.make_client()

        await adapter.create_chat_completion(
            messages=[{"role": "user", "content": "hello"}]
        )

        request = fake.completions.requests[0]
        self.assertEqual(request["model"], DEFAULT_DEEPSEEK_MODEL)
        self.assertNotIn("max_tokens", request)
        self.assertNotIn("temperature", request)

    async def test_explicit_max_tokens_is_forwarded(self) -> None:
        adapter, fake = self.make_client()

        await adapter.create_chat_completion(
            messages=[{"role": "user", "content": "hello"}], max_tokens=8_192
        )

        self.assertEqual(fake.completions.requests[0]["max_tokens"], 8_192)

    async def test_tool_json_and_reasoning_options_are_forwarded(self) -> None:
        adapter, fake = self.make_client()
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file",
                    "parameters": {"type": "object", "properties": {}},
                    "strict": True,
                },
            }
        ]

        await adapter.create_chat_completion(
            messages=[{"role": "user", "content": "read it"}],
            tools=tools,
            tool_choice="auto",
            response_format={"type": "json_object"},
            thinking="enabled",
            reasoning_effort="high",
        )

        request = fake.completions.requests[0]
        self.assertEqual(request["tools"], tools)
        self.assertEqual(request["tool_choice"], "auto")
        self.assertEqual(request["response_format"], {"type": "json_object"})
        self.assertEqual(request["reasoning_effort"], "high")
        self.assertEqual(
            request["extra_body"], {"thinking": {"type": "enabled"}}
        )

    async def test_vision_completion_uses_vision_model_and_data_url(self) -> None:
        adapter, fake = self.make_client()

        await adapter.create_vision_completion(
            prompt="Describe this slide.", image=test_image(), detail="high"
        )

        request = fake.completions.requests[0]
        self.assertEqual(request["model"], DEFAULT_DEEPSEEK_VISION_MODEL)
        content = request["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "Describe this slide."})
        expected = base64.b64encode(test_image().data).decode("ascii")
        self.assertEqual(
            content[1],
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{expected}",
                    "detail": "high",
                },
            },
        )

    def test_vision_message_rejects_empty_prompt_and_invalid_detail(self) -> None:
        with self.assertRaisesRegex(DeepSeekConfigurationError, "cannot be empty"):
            build_vision_user_message(prompt=" ", image=test_image())
        with self.assertRaisesRegex(DeepSeekConfigurationError, "Image detail"):
            build_vision_user_message(
                prompt="describe", image=test_image(), detail="full"  # type: ignore[arg-type]
            )

    async def test_empty_messages_and_invalid_max_tokens_are_rejected(self) -> None:
        adapter, _ = self.make_client()
        with self.assertRaisesRegex(DeepSeekConfigurationError, "messages"):
            await adapter.create_chat_completion(messages=[])
        for invalid_value in (0, -1):
            with (
                self.subTest(max_tokens=invalid_value),
                self.assertRaisesRegex(DeepSeekConfigurationError, "max_tokens"),
            ):
                await adapter.create_chat_completion(
                    messages=[{"role": "user", "content": "hello"}],
                    max_tokens=invalid_value,
                )

    def test_response_text_and_tool_call_only_response(self) -> None:
        text_response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="  answer  "))]
        )
        tool_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=None, tool_calls=[object()])
                )
            ]
        )

        self.assertEqual(DeepSeekClient.response_text(text_response), "answer")
        with self.assertRaisesRegex(DeepSeekResponseError, "message.tool_calls"):
            DeepSeekClient.response_text(tool_response)


if __name__ == "__main__":
    unittest.main()
