from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_context import build_agent1_context_blocks
from agent_lab import AgentLabService, AgentLabStore
from classroom_app import create_app
from config import Settings
from scoped_instructions import (
    INSTRUCTION_POLICY_LABELS,
    InstructionConflictError,
    InstructionResolver,
    InstructionStore,
    instruction_context_blocks,
)
from workspace_catalog import PreparationManager, WorkspaceCatalog

PROJECT_ROOT = Path(__file__).resolve().parents[1]


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


def _catalog(root: Path) -> WorkspaceCatalog:
    index = root / "courses" / "index.json"
    index.parent.mkdir(parents=True)
    index.write_text(
        json.dumps({"schema_version": 1, "course_ids": []}), encoding="utf-8"
    )
    return WorkspaceCatalog(root)


def _course_with_lectures(
    catalog: WorkspaceCatalog, title: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    course = catalog.create_course(title=title)
    first = catalog.create_lecture(course["id"], number="01", title="First")
    second = catalog.create_lecture(course["id"], number="02", title="Second")
    return course, first, second


def _all_scope_agent(agent_id: str = "agent1") -> dict[str, Any]:
    return {
        "agentId": agent_id,
        "contextPolicy": list(INSTRUCTION_POLICY_LABELS.values()),
    }


def _activate(
    store: InstructionStore, document_id: str, content: str
) -> dict[str, Any]:
    draft = store.create_draft(document_id)
    saved = store.save_draft(
        document_id,
        draft["id"],
        content=content,
        expected_updated_at=draft["updatedAt"],
        note=f"Set {document_id}",
    )
    return store.activate(document_id, saved["id"])


def _ready(catalog: WorkspaceCatalog, course: dict[str, Any], lecture: dict[str, Any]) -> None:
    catalog.upload_material(
        course["id"], lecture["id"], filename="slides.pdf", content=b"%PDF"
    )
    prepared = catalog.lecture_root(course["id"], lecture["id"]) / "prepared"
    prepared.mkdir()
    (prepared / "lecture_scaffold.json").write_text("# Overview", encoding="utf-8")
    (prepared / "lecture_scaffold.md").write_text("# Slides", encoding="utf-8")
    (prepared / "asr_context.txt").write_text("Lecture context", encoding="utf-8")
    (prepared / "hotwords.json").write_text("[]", encoding="utf-8")
    catalog.mark_prepared(course["id"], lecture["id"])


def test_explicit_legacy_documents_migrate_without_changing_sources(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    course, lecture, _ = _course_with_lectures(catalog, "Open Systems")
    faculty = tmp_path / "instructions" / "faculty.md"
    user = tmp_path / "instructions" / "user.md"
    faculty.parent.mkdir()
    faculty.write_text("faculty legacy", encoding="utf-8")
    user.write_text("user legacy", encoding="utf-8")
    course_source = tmp_path / "courses" / course["id"] / "course.md"
    course_source.write_text("course legacy", encoding="utf-8")

    store = InstructionStore(tmp_path, catalog)
    documents = store.list_documents()

    assert store.get_active_version("faculty")["content"] == "faculty legacy"
    assert store.get_active_version("user")["content"] == "user legacy"
    assert store.get_active_version(f"course--{course['id']}")["content"] == "course legacy"
    assert store.get_active_version(
        f"lecture--{course['id']}--{lecture['id']}"
    ) is None
    assert faculty.read_text(encoding="utf-8") == "faculty legacy"
    assert user.read_text(encoding="utf-8") == "user legacy"
    assert course_source.read_text(encoding="utf-8") == "course legacy"
    assert len(documents) == 5


def test_faculty_and_user_cross_courses_but_course_and_lecture_do_not_leak(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    course_a, lecture_a1, lecture_a2 = _course_with_lectures(catalog, "Course A")
    course_b, lecture_b1, _ = _course_with_lectures(catalog, "Course B")
    store = InstructionStore(tmp_path, catalog)
    resolver = InstructionResolver(store)
    _activate(store, "faculty", "faculty shared")
    _activate(store, "user", "user shared")
    _activate(store, f"course--{course_a['id']}", "course A only")
    _activate(
        store,
        f"lecture--{course_a['id']}--{lecture_a1['id']}",
        "lecture A1 only",
    )

    a1 = resolver.resolve(
        agent_definition=_all_scope_agent(),
        course_id=course_a["id"],
        lecture_id=lecture_a1["id"],
    )
    a2 = resolver.resolve(
        agent_definition=_all_scope_agent(),
        course_id=course_a["id"],
        lecture_id=lecture_a2["id"],
    )
    b1 = resolver.resolve(
        agent_definition=_all_scope_agent(),
        course_id=course_b["id"],
        lecture_id=lecture_b1["id"],
    )

    assert [item["content"] for item in a1["instructions"]] == [
        "faculty shared",
        "user shared",
        "course A only",
        "lecture A1 only",
    ]
    assert [item["content"] for item in a2["instructions"]] == [
        "faculty shared",
        "user shared",
        "course A only",
    ]
    assert [item["content"] for item in b1["instructions"]] == [
        "faculty shared",
        "user shared",
    ]


def test_empty_and_draft_scopes_are_skipped_and_policy_is_authoritative(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    course, lecture, _ = _course_with_lectures(catalog, "Course A")
    store = InstructionStore(tmp_path, catalog)
    resolver = InstructionResolver(store)
    _activate(store, "faculty", "faculty active")
    user_draft = store.create_draft("user")
    store.save_draft(
        "user",
        user_draft["id"],
        content="user draft must not load",
        expected_updated_at=user_draft["updatedAt"],
    )
    _activate(store, f"course--{course['id']}", "")

    resolved = resolver.resolve(
        agent_definition={
            "agentId": "agent1",
            "contextPolicy": ["Faculty Instructions", "Course Instructions"],
        },
        course_id=course["id"],
        lecture_id=lecture["id"],
    )

    assert resolved["loadOrder"] == ["faculty"]
    assert [item["content"] for item in resolved["instructions"]] == [
        "faculty active"
    ]


def test_version_conflict_restore_and_single_active(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    _course_with_lectures(catalog, "Course A")
    store = InstructionStore(tmp_path, catalog)
    first = store.create_draft("faculty")
    saved = store.save_draft(
        "faculty",
        first["id"],
        content="v1",
        expected_updated_at=first["updatedAt"],
    )
    with pytest.raises(InstructionConflictError, match="another window"):
        store.save_draft(
            "faculty",
            first["id"],
            content="stale",
            expected_updated_at=first["updatedAt"],
        )
    store.activate("faculty", saved["id"])
    second = _activate(store, "faculty", "v2")
    restored = store.restore_as_draft("faculty", "v1")

    assert len([item for item in second["versions"] if item["status"] == "active"]) == 1
    assert store.get_active_version("faculty")["content"] == "v2"
    assert restored["content"] == "v1"
    assert restored["status"] == "draft"


def test_context_blocks_preserve_load_order_metadata_and_precedence(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    course, lecture, _ = _course_with_lectures(catalog, "Course A")
    store = InstructionStore(tmp_path, catalog)
    resolver = InstructionResolver(store)
    for scope, document_id in (
        ("faculty", "faculty"),
        ("user", "user"),
        ("course", f"course--{course['id']}"),
        ("lecture", f"lecture--{course['id']}--{lecture['id']}"),
    ):
        _activate(store, document_id, f"{scope} content")

    snapshot = resolver.resolve(
        agent_definition=_all_scope_agent(),
        course_id=course["id"],
        lecture_id=lecture["id"],
    )
    blocks = instruction_context_blocks(snapshot)

    assert [block.scope for block in blocks] == [
        "faculty",
        "user",
        "course",
        "lecture",
    ]
    assert [block.version_id for block in blocks] == ["v1", "v1", "v1", "v1"]
    assert snapshot["precedence"] == [
        "agent_prompt",
        "lecture",
        "course",
        "user",
        "faculty",
    ]


def test_agent_lab_and_production_share_instruction_resolver_and_context_builder(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    course, lecture, _ = _course_with_lectures(catalog, "Course A")
    _ready(catalog, course, lecture)
    prompt_store = AgentLabStore(tmp_path, source_root=PROJECT_ROOT)
    instruction_store = InstructionStore(tmp_path, catalog)
    resolver = InstructionResolver(instruction_store)
    _activate(instruction_store, "faculty", "faculty context")
    _activate(instruction_store, f"course--{course['id']}", "course context")
    service = AgentLabService(
        prompt_store, catalog, instruction_resolver=resolver
    )

    resolved = service.resolve_context(
        {
            "agentId": "agent1",
            "courseId": course["id"],
            "lectureId": lecture["id"],
            "transcript": "current sentence",
        }
    )
    snapshot = resolver.resolve(
        agent_definition=prompt_store.get_agent("agent1", include_content=False),
        course_id=course["id"],
        lecture_id=lecture["id"],
    )
    production = build_agent1_context_blocks(
        system_prompt=prompt_store.get_active_version("agent1")["content"],
        previous_context="",
        transcript_chunk="current sentence",
        instruction_blocks=instruction_context_blocks(snapshot),
    )

    assert resolved["blocks"] == [block.to_dict() for block in production]
    assert [item["key"] for item in resolved["blocks"]] == [
        "system_prompt",
        "faculty_instructions",
        "course_instructions",
        "transcript",
    ]


def test_session_snapshot_survives_activation_pause_and_resume(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    course, lecture, _ = _course_with_lectures(catalog, "Course A")
    _ready(catalog, course, lecture)
    prompt_store = AgentLabStore(tmp_path, source_root=PROJECT_ROOT)
    instruction_store = InstructionStore(tmp_path, catalog)
    resolver = InstructionResolver(instruction_store)
    _activate(instruction_store, f"course--{course['id']}", "course v1")
    snapshot = resolver.resolve(
        agent_definition=prompt_store.get_agent("agent1", include_content=False),
        course_id=course["id"],
        lecture_id=lecture["id"],
    )
    session = catalog.start_session(
        course["id"],
        lecture["id"],
        agent1_prompt_version_id="v1",
        instruction_snapshot=snapshot,
    )
    _activate(instruction_store, f"course--{course['id']}", "course v2")

    paused = catalog.pause_session(course["id"], lecture["id"], session["id"])
    resumed = catalog.resume_session(course["id"], lecture["id"], session["id"])

    assert paused["instructionSnapshot"] == snapshot
    assert resumed["instructionSnapshot"] == snapshot
    assert resumed["instructionSnapshot"]["instructions"][-1]["content"] == "course v1"


def test_preparation_snapshots_instruction_content_before_later_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = _catalog(tmp_path)
    course, lecture, _ = _course_with_lectures(catalog, "Course A")
    catalog.upload_material(
        course["id"], lecture["id"], filename="slides.pdf", content=b"%PDF"
    )
    prompt_store = AgentLabStore(tmp_path, source_root=PROJECT_ROOT)
    instruction_store = InstructionStore(tmp_path, catalog)
    resolver = InstructionResolver(instruction_store)
    course_document = f"course--{course['id']}"
    _activate(instruction_store, course_document, "course v1")
    captured: list[dict[str, Any] | None] = []

    async def record_run(
        _course_id: str,
        _lecture_id: str,
        _prompt_version_id: str | None,
        instruction_snapshot: dict[str, Any] | None,
    ) -> None:
        captured.append(instruction_snapshot)

    async def scenario() -> dict[str, Any]:
        manager = PreparationManager(catalog, prompt_store, resolver)
        monkeypatch.setattr(manager, "_run", record_run)
        preparing = manager.start(course["id"], lecture["id"])
        _activate(instruction_store, course_document, "course v2")
        await asyncio.gather(*manager._tasks.values())
        return preparing

    preparing = asyncio.run(scenario())

    snapshot = preparing["preparation"]["instructionSnapshot"]
    assert snapshot["instructions"][-1]["content"] == "course v1"
    assert captured == [snapshot]


def test_instruction_http_api_versions_conflicts_and_preview_without_model(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    course, lecture, _ = _course_with_lectures(catalog, "Course A")
    prompt_store = AgentLabStore(tmp_path, source_root=PROJECT_ROOT)
    instruction_store = InstructionStore(tmp_path, catalog)
    resolver = InstructionResolver(instruction_store)
    client = TestClient(
        create_app(
            _settings(tmp_path),
            workspace_catalog=catalog,
            agent_lab_store=prompt_store,
            instruction_store=instruction_store,
            instruction_resolver=resolver,
        )
    )

    listed = client.get("/api/instructions/documents")
    created = client.post(
        f"/api/instructions/documents/course--{course['id']}/drafts",
        json={"note": "Course draft"},
    )
    draft = created.json()
    saved = client.put(
        f"/api/instructions/documents/course--{course['id']}/versions/{draft['id']}",
        json={
            "content": "Only this course uses packet terminology.",
            "expectedUpdatedAt": draft["updatedAt"],
            "note": "Ready",
        },
    )
    stale = client.put(
        f"/api/instructions/documents/course--{course['id']}/versions/{draft['id']}",
        json={
            "content": "stale overwrite",
            "expectedUpdatedAt": draft["updatedAt"],
        },
    )
    before_activate = client.post(
        "/api/instructions/preview-context",
        json={
            "agentId": "agent1",
            "courseId": course["id"],
            "lectureId": lecture["id"],
        },
    )
    draft_preview = client.post(
        "/api/instructions/preview-context",
        json={
            "agentId": "agent1",
            "courseId": course["id"],
            "lectureId": lecture["id"],
            "versionOverrides": {f"course--{course['id']}": draft["id"]},
        },
    )
    production_resolution = client.post(
        "/api/instructions/resolve",
        json={
            "agentId": "agent1",
            "courseId": course["id"],
            "lectureId": lecture["id"],
            "versionOverrides": {f"course--{course['id']}": draft["id"]},
        },
    )
    activated = client.post(
        f"/api/instructions/documents/course--{course['id']}/versions/{draft['id']}/activate"
    )
    preview = client.post(
        "/api/instructions/resolve",
        json={
            "agentId": "agent1",
            "courseId": course["id"],
            "lectureId": lecture["id"],
        },
    )

    assert listed.status_code == 200
    assert len(listed.json()["documents"]) == 5
    assert created.status_code == 201
    assert saved.status_code == 200
    assert stale.status_code == 409
    assert before_activate.json()["instructions"] == []
    assert draft_preview.json()["instructions"][0]["versionStatus"] == "draft"
    assert production_resolution.json()["instructions"] == []
    assert activated.json()["activeVersionId"] == draft["id"]
    assert preview.status_code == 200
    assert preview.json()["loadOrder"] == ["course"]
    assert preview.json()["blocks"][0]["scope"] == "course"
    assert preview.json()["blocks"][0]["versionId"] == draft["id"]
    assert preview.json()["blocks"][0]["source"] == "course scope"
