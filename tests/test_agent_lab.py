from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_context import (
    build_agent1_boundary_context_blocks,
    build_agent1_context_blocks,
    model_messages,
)
from agent_lab import AgentLabConflictError, AgentLabService, AgentLabStore
from classroom_app import create_app
from config import Settings
from workspace_catalog import PreparationManager, WorkspaceCatalog

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _catalog(root: Path) -> WorkspaceCatalog:
    path = root / "courses" / "index.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"schema_version": 1, "course_ids": []}), encoding="utf-8"
    )
    return WorkspaceCatalog(root)


def _ready_lecture(catalog: WorkspaceCatalog) -> tuple[dict[str, Any], dict[str, Any]]:
    course = catalog.create_course(title="Open Systems", code="COMP 3511")
    lecture = catalog.create_lecture(course["id"], number="01", title="Networks")
    catalog.upload_material(
        course["id"], lecture["id"], filename="networks.pdf", content=b"%PDF-test"
    )
    root = catalog.lecture_root(course["id"], lecture["id"])
    prepared = root / "prepared"
    prepared.mkdir()
    (prepared / "lecture_scaffold.json").write_text("# Networks", encoding="utf-8")
    (prepared / "lecture_scaffold.md").write_text("# Slides", encoding="utf-8")
    (prepared / "asr_context.txt").write_text("Lecture context", encoding="utf-8")
    (prepared / "hotwords.json").write_text("[]", encoding="utf-8")
    normalized = root / "normalized" / "normalized_slides.json"
    normalized.parent.mkdir()
    normalized.write_text(
        json.dumps({"lecture_id": lecture["id"], "slides": []}), encoding="utf-8"
    )
    catalog.mark_prepared(course["id"], lecture["id"])
    return course, catalog.get_lecture(course["id"], lecture["id"])


def _saved_draft(
    store: AgentLabStore, agent_id: str, content: str = "replacement prompt"
) -> dict[str, Any]:
    draft = store.create_draft(agent_id)
    return store.save_draft(
        agent_id,
        draft["id"],
        content=content,
        expected_updated_at=draft["updatedAt"],
        note="Test version",
    )


class FakeChatClient:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def create_chat_completion(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="dense output"))],
            usage=SimpleNamespace(
                prompt_tokens=21, completion_tokens=8, total_tokens=29
            ),
        )


def _settings(root: Path) -> Settings:
    return Settings(
        api_key="test-key",
        workspace_id=None,
        send_workspace_header=False,
        source_language="en",
        target_language="zh",
        debug=False,
        raw_event_debug=False,
        agent1_enabled=False,
        agent1_output_path=root / "data",
    )


def test_store_bootstraps_real_and_placeholder_agent_definitions(tmp_path: Path) -> None:
    store = AgentLabStore(tmp_path, source_root=PROJECT_ROOT)

    agents = store.list_agents()

    assert [item["agentId"] for item in agents] == [
        "pre-agent",
        "agent1",
        "agent1-boundary",
        "agent2",
        "agent3",
    ]
    assert store.get_active_version("pre-agent")["content"] == (
        PROJECT_ROOT / "agent_lab" / "agents" / "pre-agent" / "prompts" / "v1.md"
    ).read_text(encoding="utf-8").rstrip()
    assert store.get_active_version("agent1")["content"] == (
        PROJECT_ROOT / "agents" / "agent1.md"
    ).read_text(encoding="utf-8").rstrip()
    assert store.get_active_version("agent1-boundary")["content"] == (
        PROJECT_ROOT / "agents" / "agent1_boundary.md"
    ).read_text(encoding="utf-8").rstrip()
    assert agents[3]["executable"] is False
    assert agents[3]["promptEditable"] is True
    assert agents[3]["activePromptVersionId"] == "v1"
    assert store.get_active_version("agent2")["content"] == (
        PROJECT_ROOT / "agents" / "agent2.md"
    ).read_text(encoding="utf-8").rstrip()
    assert store.get_active_version("agent3")["content"] == (
        PROJECT_ROOT / "agents" / "agent3.md"
    ).read_text(encoding="utf-8").rstrip()


def test_model_config_can_be_changed_for_executable_agents(tmp_path: Path) -> None:
    store = AgentLabStore(tmp_path, source_root=PROJECT_ROOT)

    updated = store.update_model_config("pre-agent", model="qwen3.7-plus")
    assert updated["modelConfig"]["model"] == "qwen3.7-plus"
    assert updated["modelConfig"]["provider"] == "alibaba_cloud_model_studio"

    updated = store.update_model_config("agent1", model="qwen-plus-latest")

    assert updated["modelConfig"]["model"] == "qwen-plus-latest"
    assert updated["modelConfig"]["provider"] == "alibaba_cloud_model_studio"
    assert updated["modelConfig"]["readOnly"] is False
    assert store.get_agent("agent1")["modelConfig"]["model"] == "qwen-plus-latest"
    updated = store.update_model_config("agent1", model="deepseek-flash")
    assert updated["modelConfig"]["provider"] == "deepseek"
    with pytest.raises(ValueError, match="Model must"):
        store.update_model_config("agent1", model="invalid model name")
    with pytest.raises(ValueError, match="not executable"):
        store.update_model_config("agent2", model="future-model")


def test_agent1_default_ignores_retired_model_environment(tmp_path, monkeypatch):
    for key in ("QWEN_MODEL", "AGENT1_SEGMENTER_MODEL", "AGENT1_SUMMARIZER_MODEL"):
        monkeypatch.setenv(key, "retired-model")
    monkeypatch.setenv("QWEN_MAX_OUTPUT_TOKENS", "512")
    store = AgentLabStore(tmp_path, source_root=PROJECT_ROOT)
    config = store.get_agent("agent1")["modelConfig"]
    assert config["provider"] == "alibaba_cloud_model_studio"
    assert config["model"] == "qwen3.8-max"
    assert config["maxOutputTokens"] is None
    assert config["temperature"] == 0


def test_agent1_legacy_model_migration_preserves_versions_and_history(tmp_path):
    store = AgentLabStore(tmp_path, source_root=PROJECT_ROOT)
    _saved_draft(store, "agent1", "Preserve this user draft")
    path = tmp_path / "agent_lab/agents/agent1/agent.json"
    old = json.loads(path.read_text())
    old.pop("modelConfigSource")
    old["modelConfig"].update(provider="deepseek", model="deepseek-flash", temperature=.1)
    path.write_text(json.dumps(old))
    prompts = {item.name: item.read_bytes() for item in path.parent.glob("prompts/*.md")}
    history = tmp_path / "agent_lab/test_runs/old.json"
    history.parent.mkdir()
    history.write_text('{"model":"deepseek-flash","output":"Historical output"}')
    historical_bytes = history.read_bytes()

    store = AgentLabStore(tmp_path, source_root=PROJECT_ROOT)
    current = json.loads(path.read_text())
    assert current["modelConfig"]["model"] == "qwen3.8-max"
    assert current["modelConfigSource"] == "agent-definition"
    assert current["versions"] == old["versions"]
    assert current["activePromptVersionId"] == old["activePromptVersionId"]
    assert {item.name: item.read_bytes() for item in path.parent.glob("prompts/*.md")} == prompts
    assert history.read_bytes() == historical_bytes
    store.update_model_config("agent1", model="qwen-plus-latest")
    reloaded = AgentLabStore(tmp_path, source_root=PROJECT_ROOT)
    assert reloaded.get_agent("agent1")["modelConfig"]["model"] == "qwen-plus-latest"


def test_agent1_lab_and_postclass_runtime_share_one_model_snapshot(tmp_path, monkeypatch):
    import classroom_app

    catalog = _catalog(tmp_path)
    course, lecture = _ready_lecture(catalog)
    session = catalog.start_session(course["id"], lecture["id"])
    catalog.complete_session(course["id"], lecture["id"], session["id"])
    store = AgentLabStore(tmp_path, source_root=PROJECT_ROOT)
    fake = FakeChatClient()
    service = AgentLabService(store, catalog, clients={"agent1": fake})
    monkeypatch.setenv("AGENT1_SEGMENTER_CONTEXT_WINDOW_TOKENS", "50000")
    monkeypatch.setenv("AGENT1_SEGMENTER_MAX_OUTPUT_TOKENS", "1024")
    monkeypatch.setenv("AGENT1_SEGMENTER_MODEL", "unused-segmenter")
    monkeypatch.setenv("AGENT1_SUMMARIZER_MODEL", "unused-summarizer")
    monkeypatch.setenv("AGENT1_SEGMENTER_PROVIDER", "deepseek")
    monkeypatch.setenv("AGENT1_SUMMARIZER_PROVIDER", "deepseek")
    captured = []

    class FakeRun:
        def __init__(self, **kwargs):
            captured.append(kwargs)
            self.output_dir = kwargs["output_dir"]
            self.segmenter_spec = kwargs["segmenter_spec"]

        async def run(self):
            self.output_dir.mkdir(parents=True, exist_ok=True)
            (self.output_dir / "manifest.json").write_text('{"status":"completed"}')
            return {"status": "completed"}

    monkeypatch.setattr(classroom_app, "Agent1V1Run", FakeRun)
    monkeypatch.setattr(classroom_app, "provider_from_spec", lambda spec: fake)
    client = TestClient(create_app(_settings(tmp_path), workspace_catalog=catalog, agent_lab_store=store))
    endpoint = f"/api/courses/{course['id']}/lectures/{lecture['id']}/sessions/{session['id']}/agent1"
    with client:
        for model in ("qwen3.8-max", "qwen-plus-latest"):
            store.update_model_config("agent1", model=model)
            lab_run = asyncio.run(service.run_test({"agentId": "agent1", "courseId": course["id"], "lectureId": lecture["id"], "transcript": "Packets are forwarded."}))
            response = client.post(endpoint + "/run")
            assert response.status_code == 202
            assert client.get(endpoint).json()["status"] == "completed"
            specs = captured[-1]
            assert specs["segmenter_spec"].model == specs["summarizer_spec"].model == lab_run["model"] == model
            assert specs["segmenter_spec"].provider == specs["summarizer_spec"].provider == lab_run["modelConfigSnapshot"]["provider"] == "alibaba_cloud_model_studio"
            assert specs["segmenter_spec"].max_output_tokens == 1024
            assert specs["segmenter_spec"].context_window_tokens == 50000
            assert specs["summarizer_spec"].max_output_tokens is None
    # A later edit cannot mutate a previously constructed runtime snapshot.
    assert captured[0]["segmenter_spec"].model == "qwen3.8-max"
    assert fake.requests[-1]["model"] == "qwen-plus-latest"
    assert fake.requests[-1]["temperature"] == 0


def test_draft_save_does_not_change_active_and_stale_save_conflicts(
    tmp_path: Path,
) -> None:
    store = AgentLabStore(tmp_path, source_root=PROJECT_ROOT)
    original = store.get_active_version("agent1")
    draft = store.create_draft("agent1")
    saved = store.save_draft(
        "agent1",
        draft["id"],
        content="new draft prompt",
        expected_updated_at=draft["updatedAt"],
    )

    assert store.get_active_version("agent1")["id"] == original["id"]
    assert store.get_active_version("agent1")["content"] == original["content"]
    assert saved["status"] == "draft"
    with pytest.raises(AgentLabConflictError, match="another window"):
        store.save_draft(
            "agent1",
            draft["id"],
            content="stale overwrite",
            expected_updated_at=draft["updatedAt"],
        )


def test_non_executable_agent_prompt_can_be_versioned_without_enabling_runtime(
    tmp_path: Path,
) -> None:
    store = AgentLabStore(tmp_path, source_root=PROJECT_ROOT)
    draft = store.create_draft("agent2", note="Tune study notes")
    saved = store.save_draft(
        "agent2",
        draft["id"],
        content="Agent2 edited prompt",
        expected_updated_at=draft["updatedAt"],
    )
    activated = store.activate("agent2", saved["id"])

    assert activated["activePromptVersionId"] == "v2"
    assert store.get_active_version("agent2")["content"] == "Agent2 edited prompt"
    assert activated["executable"] is False
    assert activated["promptEditable"] is True
    with pytest.raises(ValueError, match="not executable"):
        store.update_model_config("agent2", model="future-model")


def test_activation_keeps_exactly_one_active_and_rollback_creates_draft(
    tmp_path: Path,
) -> None:
    store = AgentLabStore(tmp_path, source_root=PROJECT_ROOT)
    draft = _saved_draft(store, "agent1")

    activated = store.activate("agent1", draft["id"])
    restored = store.restore_as_draft("agent1", "v1")
    after_restore = store.get_agent("agent1")

    assert [item["id"] for item in activated["versions"] if item["status"] == "active"] == [
        draft["id"]
    ]
    assert next(item for item in activated["versions"] if item["id"] == "v1")[
        "status"
    ] == "archived"
    assert restored["id"] == "v3"
    assert restored["status"] == "draft"
    assert restored["content"] == store.get_version("agent1", "v1")["content"]
    assert after_restore["activePromptVersionId"] == draft["id"]


def test_compare_and_production_prompt_resolution_use_version_files(tmp_path: Path) -> None:
    store = AgentLabStore(tmp_path, source_root=PROJECT_ROOT)
    draft = _saved_draft(store, "pre-agent", "line one\nline two")
    comparison = store.compare("pre-agent", "v1", draft["id"])
    store.activate("pre-agent", draft["id"])

    prompt_path = store.prompt_path("pre-agent", store.get_active_version("pre-agent")["id"])

    assert comparison["added"] > 0
    assert comparison["removed"] > 0
    assert prompt_path.read_text(encoding="utf-8").strip() == "line one\nline two"


def test_agent1_context_resolution_reuses_production_builder(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    course, lecture = _ready_lecture(catalog)
    store = AgentLabStore(tmp_path, source_root=PROJECT_ROOT)
    service = AgentLabService(store, catalog)
    payload = {
        "agentId": "agent1",
        "courseId": course["id"],
        "lectureId": lecture["id"],
        "transcript": "Packets cross the router.",
        "previousContext": "Earlier context.",
    }

    resolved = service.resolve_context(payload)
    expected_blocks = build_agent1_context_blocks(
        system_prompt=store.get_active_version("agent1")["content"],
        previous_context="Earlier context.",
        transcript_chunk="Packets cross the router.",
    )

    assert resolved["blocks"] == [block.to_dict() for block in expected_blocks]
    assert resolved["messages"] == model_messages(expected_blocks)
    assert [block["key"] for block in resolved["blocks"]] == [
        "system_prompt",
        "transcript",
    ]


def test_agent1_test_run_is_explicit_persisted_and_workspace_isolated(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    course, lecture = _ready_lecture(catalog)
    store = AgentLabStore(tmp_path, source_root=PROJECT_ROOT)
    fake = FakeChatClient()
    service = AgentLabService(store, catalog, clients={"agent1": fake})
    lecture_root = catalog.lecture_root(course["id"], lecture["id"])
    before = {
        path.relative_to(lecture_root): path.read_bytes()
        for path in lecture_root.rglob("*")
        if path.is_file()
    }

    run = asyncio.run(
        service.run_test(
            {
                "agentId": "agent1",
                "courseId": course["id"],
                "lectureId": lecture["id"],
                "transcript": "A packet is stored and then forwarded.",
            }
        )
    )
    after = {
        path.relative_to(lecture_root): path.read_bytes()
        for path in lecture_root.rglob("*")
        if path.is_file()
    }

    assert len(fake.requests) == 1
    assert run["output"] == "dense output"
    assert run["usage"] == {"inputTokens": 21, "outputTokens": 8, "totalTokens": 29}
    assert run["promptContentSnapshot"] == store.get_active_version("agent1")["content"]
    assert run["modelConfigSnapshot"]["model"] == "qwen3.8-max"
    assert store.get_test_run(run["id"])["resolvedContext"] == run["resolvedContext"]
    assert store.list_test_runs("agent1")[0]["id"] == run["id"]
    assert before == after


def test_boundary_detector_context_and_test_match_production_messages(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    course, lecture = _ready_lecture(catalog)
    store = AgentLabStore(tmp_path, source_root=PROJECT_ROOT)
    fake = FakeChatClient()
    service = AgentLabService(store, catalog, clients={"agent1-boundary": fake})
    fake_output = json.dumps(
        {
            "decision": "cut",
            "cut_after_source_id": "S1",
            "boundary_type": "completed_explanation",
        }
    )

    async def boundary_response(**kwargs: Any) -> Any:
        fake.requests.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=fake_output))],
            usage=None,
        )

    fake.create_chat_completion = boundary_response  # type: ignore[method-assign]
    payload = {
        "agentId": "agent1-boundary",
        "courseId": course["id"],
        "lectureId": lecture["id"],
        "transcript": "[S1] A packet is forwarded.\n[S2] Now consider routing.",
        "hardMaxReached": True,
    }

    run = asyncio.run(service.run_test(payload))
    expected = build_agent1_boundary_context_blocks(
        system_prompt=store.get_active_version("agent1-boundary")["content"],
        transcript=payload["transcript"],
        hard_max_reached=True,
    )

    assert fake.requests[0]["messages"] == model_messages(expected)
    assert fake.requests[0]["model"] == "deepseek-flash"
    assert fake.requests[0]["response_format"] == {"type": "json_object"}
    assert run["validation"] == {"status": "valid"}


def test_pre_agent_test_reuses_normalized_input_in_isolated_workspace(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    course, lecture = _ready_lecture(catalog)
    store = AgentLabStore(tmp_path, source_root=PROJECT_ROOT)
    calls: list[tuple[Path, Path]] = []

    async def fake_runner(workspace: Path, prompt_path: Path) -> dict[str, Any]:
        calls.append((workspace, prompt_path))
        assert (workspace / "normalized" / "normalized_slides.json").is_file()
        assert (workspace / "source" / "original.pdf").is_file()
        (workspace / "prepared").mkdir()
        (workspace / "prepared" / "lecture_scaffold.json").write_text(
            "test-only", encoding="utf-8"
        )
        return {
            "output": "isolated main result",
            "validation": {"status": "valid"},
            "usage": {"agentCalls": 4},
        }

    service = AgentLabService(store, catalog, pre_agent_runner=fake_runner)
    production_overview = (
        catalog.lecture_root(course["id"], lecture["id"])
        / "prepared"
        / "lecture_scaffold.json"
    )

    run = asyncio.run(
        service.run_test(
            {
                "agentId": "pre-agent",
                "courseId": course["id"],
                "lectureId": lecture["id"],
            }
        )
    )

    assert run["validation"] == {"status": "valid"}
    assert run["resolvedContext"]["blocks"][2]["sentToModel"] is False
    assert run["input"]["normalizedPath"] == "normalized/normalized_slides.json"
    assert len(calls) == 1
    assert calls[0][0].is_relative_to(store.root / "test_workspaces")
    assert production_overview.read_text(encoding="utf-8") == "# Networks"


def test_session_and_preparation_snapshot_versions_do_not_follow_later_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = _catalog(tmp_path)
    course, lecture = _ready_lecture(catalog)
    store = AgentLabStore(tmp_path, source_root=PROJECT_ROOT)
    session = catalog.start_session(
        course["id"], lecture["id"], agent1_prompt_version_id="v1"
    )
    agent1_v2 = _saved_draft(store, "agent1")
    store.activate("agent1", agent1_v2["id"])

    assert catalog.get_session(course["id"], lecture["id"], session["id"])[
        "agent1PromptVersionId"
    ] == "v1"

    catalog.complete_session(course["id"], lecture["id"], session["id"])
    catalog.upload_material(
        course["id"], lecture["id"], filename="networks-v2.pdf", content=b"%PDF-v2"
    )
    started: list[tuple[str, str, str | None, dict[str, Any] | None]] = []

    async def record_run(
        course_id: str,
        lecture_id: str,
        prompt_version_id: str | None,
        instruction_snapshot: dict[str, Any] | None,
    ) -> None:
        started.append(
            (course_id, lecture_id, prompt_version_id, instruction_snapshot)
        )

    async def scenario() -> dict[str, Any]:
        manager = PreparationManager(catalog, store)
        monkeypatch.setattr(manager, "_run", record_run)
        preparing = manager.start(course["id"], lecture["id"])
        pre_v2 = _saved_draft(store, "pre-agent")
        store.activate("pre-agent", pre_v2["id"])
        await asyncio.gather(*manager._tasks.values())
        return preparing

    preparing = asyncio.run(scenario())

    assert preparing["preparation"]["preAgentPromptVersionId"] == "v1"
    assert started == [(course["id"], lecture["id"], "v1", None)]
    assert catalog.get_lecture(course["id"], lecture["id"])["preparation"][
        "preAgentPromptVersionId"
    ] == "v1"


def test_agent_lab_api_exposes_separate_version_context_and_test_endpoints(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    course, lecture = _ready_lecture(catalog)
    store = AgentLabStore(tmp_path, source_root=PROJECT_ROOT)
    fake = FakeChatClient()
    service = AgentLabService(store, catalog, clients={"agent1": fake})
    client = TestClient(
        create_app(
            _settings(tmp_path),
            workspace_catalog=catalog,
            agent_lab_store=store,
            agent_lab_service=service,
        )
    )

    listed = client.get("/api/agent-lab/agents")
    configured = client.put(
        "/api/agent-lab/agents/agent1/model-config",
        json={"model": "qwen-plus-latest"},
    )
    versions = client.get("/api/agent-lab/agents/agent1/versions")
    created = client.post(
        "/api/agent-lab/agents/agent1/drafts", json={"note": "API draft"}
    )
    draft = created.json()
    saved = client.put(
        f"/api/agent-lab/agents/agent1/versions/{draft['id']}",
        json={
            "content": "API prompt",
            "expectedUpdatedAt": draft["updatedAt"],
            "note": "saved",
        },
    )
    stale = client.put(
        f"/api/agent-lab/agents/agent1/versions/{draft['id']}",
        json={
            "content": "stale prompt",
            "expectedUpdatedAt": draft["updatedAt"],
        },
    )
    context_payload = {
        "agentId": "agent1",
        "promptVersionId": draft["id"],
        "courseId": course["id"],
        "lectureId": lecture["id"],
        "transcript": "The router forwards a packet.",
    }
    resolved = client.post("/api/agent-lab/context/resolve", json=context_payload)
    tested = client.post("/api/agent-lab/tests", json=context_payload)
    history = client.get("/api/agent-lab/tests?agent_id=agent1")
    fetched = client.get(f"/api/agent-lab/tests/{tested.json()['id']}")
    agent2_created = client.post(
        "/api/agent-lab/agents/agent2/drafts", json={"note": "Agent2 API draft"}
    )
    agent2_draft = agent2_created.json()
    agent2_saved = client.put(
        f"/api/agent-lab/agents/agent2/versions/{agent2_draft['id']}",
        json={
            "content": "Agent2 API prompt",
            "expectedUpdatedAt": agent2_draft["updatedAt"],
        },
    )
    agent2_activated = client.post(
        f"/api/agent-lab/agents/agent2/versions/{agent2_draft['id']}/activate"
    )
    agent2_context = client.post(
        "/api/agent-lab/context/resolve",
        json={
            "agentId": "agent2",
            "courseId": course["id"],
            "lectureId": lecture["id"],
        },
    )

    assert listed.status_code == 200
    assert configured.status_code == 200
    assert configured.json()["modelConfig"]["model"] == "qwen-plus-latest"
    assert len(listed.json()["agents"]) == 5
    assert versions.json()["versions"][0]["id"] == "v1"
    assert created.status_code == 201
    assert saved.json()["content"] == "API prompt"
    assert stale.status_code == 409
    assert resolved.status_code == 200
    assert resolved.json()["messages"][0]["content"] == "API prompt"
    assert tested.status_code == 201
    assert len(fake.requests) == 1
    assert fake.requests[0]["model"] == "qwen-plus-latest"
    assert history.json()["tests"][0]["id"] == tested.json()["id"]
    assert fetched.json()["promptContentSnapshot"] == "API prompt"
    assert agent2_created.status_code == 201
    assert agent2_saved.json()["content"] == "Agent2 API prompt"
    assert agent2_activated.json()["activePromptVersionId"] == "v2"
    assert agent2_activated.json()["executable"] is False
    assert agent2_context.status_code == 400
    assert agent2_context.json()["detail"] == "Agent is not executable yet"
