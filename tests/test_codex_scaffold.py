"""Codex transport tests: fake CLI only, no account or API requests."""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import codex_scaffold
from codex_scaffold import CodexChatClient, CodexScaffoldModel, CodexUnavailableError
from workspace_catalog import (
    PreparationManager,
    WorkspaceCatalog,
    WorkspaceCatalogError,
)


@pytest.fixture
def cli(monkeypatch):
    monkeypatch.setattr(codex_scaffold, "require_codex_login", lambda: ("/fake/codex", "codex-cli test"))
    monkeypatch.delenv("PRE_AGENT_CODEX_MODEL", raising=False)
    calls = []

    async def launch(*command, **options):
        record = {"command": command, "options": options}
        calls.append(record)

        async def communicate(prompt):
            record["prompt"] = prompt.decode()
            output = Path(command[command.index("--output-last-message") + 1])
            record["root"] = output.parent
            record["images"] = [Path(command[index + 1]).read_bytes() for index, value in enumerate(command) if value == "--image"]
            if "--output-schema" in command:
                record["schema"] = json.loads(Path(command[command.index("--output-schema") + 1]).read_text())
            output.write_text('{"ok":true}')
            return None, None

        process = SimpleNamespace(returncode=0, communicate=communicate)
        record["process"] = process
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    return calls


def request(content="Evidence"):
    return {"messages": [{"role": "system", "content": "Production instructions"}, {"role": "user", "content": content}], "max_tokens": 4096, "extra_body": {"enable_thinking": False}}


def test_codex_isolated_command_and_environment(cli, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("CODEX_API_KEY", "must-not-leak")
    monkeypatch.setenv("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "Codex Desktop")
    client = CodexChatClient()
    result = asyncio.run(client.create_chat_completion(**request()))
    assert json.loads(result.choices[0].message.content) == {"ok": True}
    call = cli[0]
    command = call["command"]
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--ephemeral" in command and "--ignore-user-config" in command
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert "4096" not in command and "max_tokens" not in call["prompt"]
    assert "Production instructions" in call["prompt"] and "Evidence" in call["prompt"]
    assert "OPENAI_API_KEY" not in call["options"]["env"]
    assert "CODEX_API_KEY" not in call["options"]["env"]
    assert "CODEX_INTERNAL_ORIGINATOR_OVERRIDE" not in call["options"]["env"]
    assert not call["root"].exists()


def test_schema_and_explicit_model_forwarded(cli, monkeypatch):
    monkeypatch.setenv("PRE_AGENT_CODEX_MODEL", "test-codex-model")
    client = CodexChatClient()
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
    asyncio.run(client.create_chat_completion(**request(), response_format={"type": "json_schema", "json_schema": {"schema": schema}}))
    assert cli[0]["schema"] == schema
    command = cli[0]["command"]
    assert command[command.index("--model") + 1] == "test-codex-model"
    assert "test-codex-model" in client.model_name


def test_reasoning_effort_is_forwarded_and_separates_cache_identity(cli):
    default = CodexChatClient("gpt-6.1-sol")
    medium = CodexChatClient("gpt-6.1-sol", reasoning_effort="medium")
    asyncio.run(medium.create_chat_completion(**request()))
    assert 'model_reasoning_effort="medium"' in cli[0]["command"]
    assert medium.model_name != default.model_name


def test_agent2_codex_binds_recorded_model_and_never_constructs_qwen(cli, monkeypatch):
    from agent1_v1 import ModelSpec
    from agent2_asr import provider_for_agent2
    import qwen_chat_client
    monkeypatch.setenv("PRE_AGENT_CODEX_MODEL", "different-preagent-model")
    monkeypatch.setenv("AGENT2_MODEL_TIMEOUT_SECONDS", "750")
    monkeypatch.setattr(qwen_chat_client, "QwenChatClient", lambda *_: pytest.fail("Unexpected Qwen fallback"))
    spec = ModelSpec("codex", "test-agent2-model", 128000, 8192)
    provider = provider_for_agent2(spec)
    assert provider.config.timeout_seconds == 750
    result = asyncio.run(provider.create_chat_completion(**request()))
    assert json.loads(result.choices[0].message.content) == {"ok": True}
    command = cli[0]["command"]
    assert command[command.index("--model") + 1] == spec.model


@pytest.mark.parametrize("model", ["", "default"])
def test_agent2_codex_rejects_ambiguous_model(model):
    from agent1_v1 import ModelSpec
    from agent2_asr import provider_for_agent2
    from agent2_retrieval import Agent2Error
    with pytest.raises(Agent2Error, match="explicit model"):
        provider_for_agent2(ModelSpec("codex", model, 128000, 8192))


def test_multiple_whole_slide_images_forwarded_in_order(cli):
    pngs = [b"\x89PNG\r\n\x1a\nslide13", b"\x89PNG\r\n\x1a\nslide15"]
    content = [{"type": "text", "text": "Slides 13 and 15"}]
    for png in pngs:
        content.append({"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}})
    asyncio.run(CodexChatClient().create_chat_completion(**request(content)))
    assert cli[0]["images"] == pngs
    assert "[Attached image 1]" in cli[0]["prompt"] and "[Attached image 2]" in cli[0]["prompt"]
    assert "base64" not in cli[0]["prompt"]


@pytest.mark.parametrize("url", ["https://example.com/a.png", "data:image/png;base64,bm90LXBuZw=="])
def test_invalid_image_is_rejected_before_cli(cli, url):
    content = [{"type": "image_url", "image_url": {"url": url}}]
    with pytest.raises(ValueError):
        asyncio.run(CodexChatClient().create_chat_completion(**request(content)))
    assert cli == []


@pytest.mark.parametrize("failure", ["exit", "missing", "invalid", "array"])
def test_cli_failure_never_falls_back(cli, monkeypatch, failure):
    async def launch(*command, **options):
        async def communicate(_prompt):
            path = Path(command[command.index("--output-last-message") + 1])
            if failure == "exit":
                options["stderr"].write(b"Codex usage limit reached")
            elif failure != "missing":
                path.write_text("[]" if failure == "array" else "not JSON")
        return SimpleNamespace(returncode=1 if failure == "exit" else 0, communicate=communicate)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    with pytest.raises((RuntimeError, ValueError, TypeError)):
        asyncio.run(CodexChatClient().create_chat_completion(**request()))


def test_usage_limit_has_typed_error_for_pipeline_stop(cli, monkeypatch):
    async def launch(*command, **options):
        async def communicate(_prompt):
            options["stderr"].write("ERROR: You’ve hit your usage limit. Try again at 10:08 PM.".encode())
        return SimpleNamespace(returncode=1, communicate=communicate)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    with pytest.raises(codex_scaffold.CodexUsageLimitError) as error:
        asyncio.run(CodexChatClient().create_chat_completion(**request()))
    assert error.value.error_type == "CODEX_USAGE_LIMIT"


@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_and_cancellation_reap_child(cli, monkeypatch, cancel):
    killed, reaped = [], []
    async def communicate(_prompt):
        if cancel:
            raise asyncio.CancelledError
        await asyncio.Future()
    async def wait():
        reaped.append(True)
    async def launch(*_command, **_options):
        return SimpleNamespace(pid=42, communicate=communicate, wait=wait)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(codex_scaffold.os, "killpg", lambda pid, sig: killed.append(pid))
    with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
        asyncio.run(CodexChatClient().create_chat_completion(**request(), timeout=.001))
    assert killed == [42] and reaped == [True]


def test_no_qwen_client_constructed(cli, monkeypatch):
    import qwen_chat_client
    def forbidden():
        pytest.fail("Codex must never create a Qwen client")
    monkeypatch.setattr(qwen_chat_client, "QwenChatClient", forbidden)
    result = asyncio.run(CodexScaffoldModel(base_prompt="Active prompt").generate("covering_relations", {"remaining_budget": 0, "nodes": [], "relations": []}))
    assert result == {"ok": True}
    assert "Active prompt" in cli[0]["prompt"]


@pytest.mark.parametrize("visual", [False, True])
def test_enrichment_does_not_instruct_codex_to_copy_empty_content(cli, tmp_path, visual):
    # The Networking Basics failure returned this exact empty template twice,
    # while putting MAC/ARP facts exclusively in evidence.
    from knowledge_scaffold import ScaffoldValidationError, empty_content
    from preagent_scaffold import _validated_content_response

    payload = {
        "node": {"node_id": "KN_0005", "title": "MAC Addresses", "node_type": "concept",
                 "scope": "MAC addressing and local MAC-to-IP correspondence"},
        "slides": [{"page": 10, "text": "MAC address (layer 2): locating a communication device "
                    "on a local network. The ARP table maintains MAC-to-IP correspondence."}],
        "validation_feedback": {"error": "CONTENT_UNDERPOPULATED"},
    }
    if visual:
        (tmp_path / "slide.png").write_bytes(b"\x89PNG\r\n\x1a\nslide10")
        payload["slides"][0]["image_path"] = "slide.png"
    asyncio.run(CodexScaffoldModel(workspace_root=tmp_path).generate("enriching_nodes", payload))
    prompt = cli[0]["prompt"]
    assert "exactly this empty shape" not in prompt
    assert json.dumps(empty_content("concept")) not in prompt
    assert "Populate the fields with facts explicitly supported by the supplied slides" in prompt
    assert "Use null or [] only for fields without supported facts" in prompt
    assert "CONTENT_UNDERPOPULATED" in prompt
    # Visual tasks use JSON mode, so field types must also reach Codex in the
    # prompt. Otherwise it can put a single mechanism into a bare string.
    assert '"mechanism": {"type": "array", "items": {"type": "string"}}' in prompt
    assert "an array field must remain an array even when it contains one fact" in prompt
    if not visual:
        schema = cli[0]["schema"]["properties"]["content"]
        assert set(schema["required"]) == set(empty_content("concept"))
        assert schema["additionalProperties"] is False
    with pytest.raises(ScaffoldValidationError, match="CONTENT_UNDERPOPULATED"):
        _validated_content_response({"content": empty_content("concept"), "evidence": [
            {"kind": "slide_example", "label": None,
             "text": "A MAC address locates a communication device on a local network."},
        ]}, "concept")


@pytest.mark.parametrize("status", ["Not logged in", "Logged in using an API key"])
def test_subscription_mode_rejects_missing_or_api_key_login(monkeypatch, status):
    monkeypatch.setattr(codex_scaffold.shutil, "which", lambda _: "/fake/codex")
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=status, stderr=""))
    with pytest.raises(CodexUnavailableError, match="ChatGPT login"):
        codex_scaffold.require_codex_login()


def test_missing_codex_has_actionable_error(monkeypatch):
    monkeypatch.setattr(codex_scaffold.shutil, "which", lambda _: None)
    monkeypatch.setattr(Path, "is_file", lambda _: False)
    with pytest.raises(CodexUnavailableError, match="not installed"):
        codex_scaffold.require_codex_login()


def test_backend_snapshot_and_preflight(cli, tmp_path, monkeypatch):
    (tmp_path / "courses").mkdir()
    (tmp_path / "courses/index.json").write_text('{"schema_version":1,"course_ids":[]}')
    catalog = WorkspaceCatalog(tmp_path)
    course = catalog.create_course(title="Test")
    lecture = catalog.create_lecture(course["id"], number="01", title="Intro")
    catalog.upload_material(course["id"], lecture["id"], filename="intro.pdf", content=b"%PDF")
    manager = PreparationManager(catalog)
    async def noop(*_args):
        pass
    monkeypatch.setattr(manager, "_run", noop)
    async def scenario():
        result = manager.start(course["id"], lecture["id"], backend="codex")
        await asyncio.gather(*manager._tasks.values())
        return result
    result = asyncio.run(scenario())
    assert result["preparation"]["executionBackend"] == "codex"
    catalog.mark_preparation_failed(course["id"], lecture["id"], "previous failure")
    def unavailable():
        raise CodexUnavailableError("Please sign in")
    monkeypatch.setattr(codex_scaffold, "require_codex_login", unavailable)
    with pytest.raises(WorkspaceCatalogError, match="Please sign in"):
        manager.start(course["id"], lecture["id"], backend="codex")
    assert catalog.get_lecture(course["id"], lecture["id"])["preparationStatus"] == "failed"
    with pytest.raises(WorkspaceCatalogError, match="backend"):
        manager.start(course["id"], lecture["id"], backend="unknown")
