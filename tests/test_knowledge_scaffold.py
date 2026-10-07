"""Deterministic PreAgent replacement contract, without paid model calls."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest

from knowledge_scaffold import (
    ScaffoldValidationError,
    compact_typed_content,
    dedupe_candidates,
    derive_asr_context,
    derive_hotwords,
    empty_content,
    empty_prelecture,
    merge_typed_content,
    normalize_relations,
    normalize_scaffold,
    render_scaffold_markdown,
    validate_scaffold,
    validate_content,
)
from preagent_scaffold import BUILD_SCAFFOLD_TIMEOUT_SECONDS, PreAgentError, PreAgentRun, QwenScaffoldModel, _response_format, _validated_content_response, _validated_prelecture_response, bounded_batches, resolve_committed_package
from workspace_catalog import WorkspaceCatalog


class MockModel:
    model_name = "mock"

    def __init__(self, *, fail_stage: str | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail_stage = fail_stage

    async def generate(self, stage: str, payload: dict) -> dict:
        self.calls.append((stage, payload))
        if stage == self.fail_stage:
            raise RuntimeError("provider root cause")
        if stage == "understanding_slides":
            return {"descriptors": [{"page": item["page"], "title": f"Slide {item['page']}", "topics": ["Accounting"], "concepts": ["Balance Sheet"], "formulas": [], "examples": [], "role": "concept_explanation"} for item in payload["slides"]]}
        if stage == "extracting_knowledge":
            pages = [item["page"] for item in payload["slides"]]
            return {"candidates": [
                {"title": "Accounting", "suggested_type": "topic", "aliases": [], "source_pages": pages},
                {"title": "Balance Sheet", "suggested_type": "concept", "aliases": ["Statement of financial position"], "source_pages": pages},
            ]}
        if stage == "building_scaffold":
            assert len(payload["candidates"]) == 2
            return {"nodes": [
                {"key": "accounting", "title": "Accounting", "node_type": "topic", "parent_key": None, "candidate_ids": ["KC_0001"], "aliases": [], "scope": "Organizes the balance sheet topic."},
                {"key": "balance", "title": "Balance Sheet", "node_type": "concept", "parent_key": "accounting", "candidate_ids": ["KC_0002"], "aliases": ["Statement of financial position"], "scope": "Assets on the balance sheet."},
            ], "relations": []}
        if stage == "enriching_nodes":
            assert len(payload["slides"]) <= 3
            return {"content": {**empty_content("concept"), "definition": "A slide-supported accounting concept.", "key_terms": ["Assets"]}, "evidence": []}
        if stage == "covering_relations":
            return {"relations": []}
        raise AssertionError(stage)


def workspace(tmp_path: Path, count: int = 45) -> Path:
    normalized = tmp_path / "normalized/normalized_slides.json"
    normalized.parent.mkdir(parents=True)
    normalized.write_text(json.dumps({"slides": [{"slide_number": page, "blocks": [{"text": f"Balance Sheet content page {page}", "include_in_semantic_context": True}]} for page in range(1, count + 1)]}), encoding="utf-8")
    source = tmp_path / "source/original.pdf"
    source.parent.mkdir(exist_ok=True)
    source.write_bytes(b"%PDF-mock")
    return tmp_path


def run(tmp_path: Path, model: MockModel, count: int = 45) -> dict:
    workspace(tmp_path, count)
    asyncio.run(PreAgentRun(tmp_path, "lecture-1", "Accounting 101", model).run())
    return json.loads((tmp_path / "prepared/lecture_scaffold.json").read_text())


def test_codex_backend_reuses_production_tasks_and_isolates_qwen_cache(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import codex_scaffold
    from workspace_catalog import PreparationManager

    mock_model = MockModel()
    # Seed a different provider's cache. Codex must run its own requests.
    run(tmp_path, mock_model, count=3)
    catalog_root = tmp_path / "catalog"
    (catalog_root / "courses").mkdir(parents=True)
    (catalog_root / "courses/index.json").write_text('{"schema_version":1,"course_ids":[]}')
    catalog = WorkspaceCatalog(catalog_root)
    course = catalog.create_course(title="Accounting")
    lecture = catalog.create_lecture(course["id"], number="01", title="Accounting 101")
    catalog.upload_material(course["id"], lecture["id"], filename="slides.pdf", content=b"%PDF-mock")
    root = catalog.lecture_root(course["id"], lecture["id"])
    workspace(root, count=3)
    # Copy validated cache entries only; do not reuse any generated artifacts.
    import shutil
    shutil.copytree(tmp_path / "runtime/preagent_cache", root / "runtime/preagent_cache")
    stages = []

    class FakeCodexClient:
        def __init__(self, *, reasoning_effort=None):
            self.reasoning_effort = reasoning_effort

        model_name = "codex-cli/fake/default"
        config = SimpleNamespace(timeout_seconds=300)

        async def create_chat_completion(self, **request):
            system = request["messages"][0]["content"]
            payload = json.loads(request["messages"][1]["content"])
            if "exactly one descriptor" in system:
                stage = "understanding_slides"
            elif "containing an array" in system:
                stage = "extracting_knowledge"
            elif "nodes:[{key" in system:
                stage = "building_scaffold"
            elif "content and evidence" in system:
                stage = "enriching_nodes"
            else:
                stage = "covering_relations"
            stages.append(stage)
            content = json.dumps(await mock_model.generate(stage, payload))
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    monkeypatch.setattr(codex_scaffold, "CodexChatClient", FakeCodexClient)
    catalog.mark_preparing(course["id"], lecture["id"], execution_backend="codex")
    manager = PreparationManager(catalog)
    # Manager initialization recovers stale preparing flags; restore this live run.
    catalog.mark_preparing(course["id"], lecture["id"], execution_backend="codex")
    monkeypatch.setattr(manager, "_parse_material", lambda *_args: None)
    asyncio.run(manager._run(course["id"], lecture["id"], None, None))
    assert catalog.get_lecture(course["id"], lecture["id"])["preparationStatus"] == "ready"
    assert set(stages) == {"understanding_slides", "extracting_knowledge", "building_scaffold", "enriching_nodes", "covering_relations"}
    scaffold = json.loads((root / "prepared/lecture_scaffold.json").read_text())
    assert scaffold["nodes"]["KN_0002"]["content"]["definition"]
    assert resolve_committed_package(root)["run_id"] == scaffold["metadata"]["preagent_run_id"]
    assert (root / "prepared/hotwords.json").is_file()
    assert (root / "prepared/asr_context.txt").is_file()


def test_45_page_pipeline_batches_dedupes_and_commits(tmp_path: Path) -> None:
    model = MockModel()
    scaffold = run(tmp_path, model)
    stages = [stage for stage, _ in model.calls]
    understanding = [payload for stage, payload in model.calls if stage == "understanding_slides"]
    extraction = [payload for stage, payload in model.calls if stage == "extracting_knowledge"]
    enrichment = [payload for stage, payload in model.calls if stage == "enriching_nodes"]
    assert len(understanding) >= 12
    assert all(len(payload["slides"]) <= 4 for payload in understanding)
    assert all(len(payload["slides"]) <= 8 for payload in extraction)
    assert len(enrichment) == 15
    assert stages.count("building_scaffold") == 1
    assert list(scaffold["nodes"]) == ["KN_0001", "KN_0002"]
    assert scaffold["root_nodes"] == ["KN_0001"]
    assert scaffold["nodes"]["KN_0001"]["children"] == ["KN_0002"]
    assert len(scaffold["nodes"]["KN_0002"]["source_refs"]) == 45
    assert scaffold["schema_version"] == "2.0"
    assert scaffold["metadata"]["prompt_version"] == "v2"
    assert scaffold["nodes"]["KN_0001"]["content"] == empty_content("topic")
    assert scaffold["nodes"]["KN_0002"]["scope"] == "Assets on the balance sheet."
    assert resolve_committed_package(tmp_path)["run_id"] == scaffold["metadata"]["preagent_run_id"]
    assert len((tmp_path / "intermediate/slide_descriptors.jsonl").read_text().splitlines()) == 45
    assert len((tmp_path / "intermediate/knowledge_candidates.jsonl").read_text().splitlines()) == 2
    assert not list(tmp_path.rglob("sec_*.json"))
    manifest = json.loads(next((tmp_path / "runtime/runs").glob("*/manifest.json")).read_text())
    assert "prepared/lecture_scaffold.json" in [item["path"] for item in manifest["artifacts"]]
    assert "expected_section_ids" not in manifest
    assert "Balance Sheet" in [item["term"] for item in derive_hotwords(scaffold)["hotwords"]]
    assert "Assets" not in [item["term"] for item in derive_hotwords(scaffold)["hotwords"]]
    assert len(derive_asr_context(scaffold)) <= 300
    assert render_scaffold_markdown(scaffold) == (tmp_path / "prepared/lecture_scaffold.md").read_text()


def test_candidate_dedupe_and_workload_limit() -> None:
    candidates = [
        {"candidate_id": "KC_0001", "title": "Balance Sheet", "suggested_type": "concept", "aliases": ["Financial Position"], "source_pages": [1]},
        {"candidate_id": "KC_0002", "title": " balance  sheet ", "suggested_type": "concept", "aliases": ["financial position"], "source_pages": [2]},
    ]
    assert dedupe_candidates(candidates)[0]["source_pages"] == [1, 2]
    assert dedupe_candidates(candidates)[0]["aliases"] == ["Financial Position"]
    batches = bounded_batches([{"page": x, "text": "a" * 100} for x in range(1, 42)], max_items=4, max_bytes=500)
    assert all(len(batch) <= 4 for batch in batches)
    assert sum(map(len, batches)) == 41
    with pytest.raises(ValueError, match="single slide"):
        bounded_batches([{"text": "a" * 1000}], max_items=4, max_bytes=500)


@pytest.mark.parametrize("mutation", [
    lambda x: x["nodes"]["KN_0002"].update(parent_id="KN_9999"),
    lambda x: x["nodes"]["KN_0001"].update(children=[]),
    lambda x: x.update(root_nodes=["KN_0002"]),
    lambda x: x["nodes"]["KN_0002"]["source_refs"].append({"type": "slide", "page": 46}),
    lambda x: x["relations"].append({"relation_id": "REL-0001", "from_node_id": "KN_0001", "to_node_id": "KN_9999", "type": "related_to", "reason": "Cited by slides.", "source_refs": []}),
    lambda x: x["nodes"]["KN_0001"].update(parent_id="KN_0002"),
    lambda x: x["nodes"]["KN_0002"].update(teacher_explanation="invented"),
])
def test_invalid_tree_is_rejected(tmp_path: Path, mutation) -> None:
    scaffold = run(tmp_path, MockModel(), 4)
    broken = deepcopy(scaffold)
    mutation(broken)
    with pytest.raises(ScaffoldValidationError):
        validate_scaffold(broken)


def test_worker_failure_preserves_root_cause_and_attempts(tmp_path: Path) -> None:
    workspace(tmp_path, 3)
    with pytest.raises(PreAgentError, match="provider root cause"):
        asyncio.run(PreAgentRun(tmp_path, "lecture-1", "Accounting", MockModel(fail_stage="understanding_slides")).run())
    state = json.loads((tmp_path / "runtime/preagent_state.json").read_text())
    assert state["status"] == "failed"
    assert state["current_stage"] == "understanding_slides"
    assert state["stages"]["understanding_slides"]["attempts"] == 2
    assert "provider root cause" in state["error"]


def test_invalid_descriptor_gets_schema_feedback_before_second_attempt(tmp_path: Path) -> None:
    class RepairingModel(MockModel):
        async def generate(self, stage: str, payload: dict) -> dict:
            if stage == "understanding_slides" and "validation_feedback" not in payload:
                self.calls.append((stage, payload))
                return {"descriptors": [
                    {"page": item["page"], "title": None, "topics": [], "concepts": [], "formulas": [], "examples": [], "role": "invented_role"}
                    for item in payload["slides"]
                ]}
            return await super().generate(stage, payload)

    model = RepairingModel()
    run(tmp_path, model, 4)
    attempts = [payload for stage, payload in model.calls if stage == "understanding_slides"]
    assert len(attempts) == 2
    assert "validation_feedback" not in attempts[0]
    assert "descriptor: invalid role" in attempts[1]["validation_feedback"]["error"]
    state = json.loads((tmp_path / "runtime/preagent_state.json").read_text())
    assert state["stages"]["understanding_slides"]["attempts"] == 2
    diagnostic = tmp_path / state["stages"]["understanding_slides"]["diagnostic_path"]
    assert json.loads(diagnostic.read_text())["response"]["descriptors"][0]["role"] == "invented_role"
    assert state["status"] == "completed"


def test_absent_descriptor_lists_do_not_consume_retry(tmp_path: Path) -> None:
    class SparseModel(MockModel):
        async def generate(self, stage: str, payload: dict) -> dict:
            result = await super().generate(stage, payload)
            if stage == "understanding_slides":
                for descriptor in result["descriptors"]:
                    descriptor.pop("formulas")
                    descriptor["examples"] = None
                    descriptor["topics"] = "Accounting"
            return result

    model = SparseModel()
    run(tmp_path, model, 4)
    assert len([stage for stage, _ in model.calls if stage == "understanding_slides"]) == 1
    rows = [json.loads(line) for line in (tmp_path / "intermediate/slide_descriptors.jsonl").read_text().splitlines()]
    assert rows[0]["formulas"] == []
    assert rows[0]["examples"] == []
    assert rows[0]["topics"] == ["Accounting"]


def test_verbose_slide_examples_are_bounded_before_validation(tmp_path: Path) -> None:
    class VerboseModel(MockModel):
        async def generate(self, stage: str, payload: dict) -> dict:
            result = await super().generate(stage, payload)
            if stage == "understanding_slides":
                for descriptor in result["descriptors"]:
                    descriptor["examples"] = [f"Example {index}: " + "x" * 200 for index in range(25)]
            return result

    run(tmp_path, VerboseModel(), 4)
    descriptors = [json.loads(line) for line in (tmp_path / "intermediate/slide_descriptors.jsonl").read_text().splitlines()]
    assert len(descriptors[0]["examples"]) == 20
    assert all(len(example) <= 160 for example in descriptors[0]["examples"])


def test_invalid_candidate_type_gets_allowed_values_before_second_attempt(tmp_path: Path) -> None:
    class RepairingModel(MockModel):
        async def generate(self, stage: str, payload: dict) -> dict:
            if stage == "extracting_knowledge" and "validation_feedback" not in payload:
                self.calls.append((stage, payload))
                return {"candidates": [{"title": "Balance Sheet", "suggested_type": "example", "aliases": [], "source_pages": [1]}]}
            return await super().generate(stage, payload)

    model = RepairingModel()
    run(tmp_path, model, 4)
    attempts = [payload for stage, payload in model.calls if stage == "extracting_knowledge"]
    assert len(attempts) == 2
    assert "validation_feedback" not in attempts[0]
    assert "invalid value 'example'" in attempts[1]["validation_feedback"]["error"]
    assert "allowed values: comparison, concept, formula, procedure, relationship, topic" in attempts[1]["validation_feedback"]["error"]
    state = json.loads((tmp_path / "runtime/preagent_state.json").read_text())
    assert state["stages"]["extracting_knowledge"]["attempts"] == 2
    assert state["status"] == "completed"


def test_candidate_aliases_and_pages_are_normalized_without_retry(tmp_path: Path) -> None:
    class DuplicatingModel(MockModel):
        async def generate(self, stage: str, payload: dict) -> dict:
            result = await super().generate(stage, payload)
            if stage == "extracting_knowledge":
                result["candidates"][1]["aliases"] = [" Assets ", "assets"]
                result["candidates"][1]["source_pages"] = [2, 1, 2]
            return result

    model = DuplicatingModel()
    run(tmp_path, model, 2)
    assert len([stage for stage, _ in model.calls if stage == "extracting_knowledge"]) == 1
    rows = [json.loads(line) for line in (tmp_path / "intermediate/knowledge_candidates.jsonl").read_text().splitlines()]
    assert rows[1]["aliases"] == ["Assets"]
    assert rows[1]["source_pages"] == [1, 2]


def test_builder_retries_with_specific_unknown_candidate_id(tmp_path: Path) -> None:
    class RepairingModel(MockModel):
        async def generate(self, stage: str, payload: dict) -> dict:
            if stage == "building_scaffold" and "validation_feedback" not in payload:
                self.calls.append((stage, payload))
                return {"nodes": [{
                    "key": "accounting", "title": "Accounting", "node_type": "topic",
                    "parent_key": None, "candidate_ids": ["KC_9999"], "aliases": [],
                    "scope": "Accounting topic.",
                }], "relations": []}
            return await super().generate(stage, payload)

    model = RepairingModel()
    run(tmp_path, model, 4)
    attempts = [payload for stage, payload in model.calls if stage == "building_scaffold"]
    assert len(attempts) == 2
    assert attempts[0]["allowed_candidate_ids"] == ["KC_0001", "KC_0002"]
    assert "builder node accounting: unknown candidate_ids ['KC_9999']" in attempts[1]["validation_feedback"]["error"]
    assert json.loads((tmp_path / "runtime/preagent_state.json").read_text())["status"] == "completed"


def test_enrichment_retries_when_concept_has_formula_variables(tmp_path: Path) -> None:
    class RepairingModel(MockModel):
        async def generate(self, stage: str, payload: dict) -> dict:
            if stage == "enriching_nodes" and "validation_feedback" not in payload:
                self.calls.append((stage, payload))
                return {"content": {**empty_content("concept"), "variables": []}, "evidence": []}
            return await super().generate(stage, payload)

    model = RepairingModel()
    run(tmp_path, model, 2)
    attempts = [payload for stage, payload in model.calls if stage == "enriching_nodes"]
    assert len(attempts) == 2
    assert "concept content: invalid fields" in attempts[1]["validation_feedback"]["error"]
    assert json.loads((tmp_path / "runtime/preagent_state.json").read_text())["status"] == "completed"


def test_enrichment_accepts_safe_envelopes_and_rejects_bad_fields() -> None:
    fields = {**empty_prelecture(), "core_idea": "A slide-supported fact."}
    assert _validated_prelecture_response(fields) == fields
    assert _validated_prelecture_response({"prelecture": fields, "node_id": "KN_0014"}) == fields
    with pytest.raises(ScaffoldValidationError, match="got keys .*node_id"):
        _validated_prelecture_response({"node_id": "KN_0014"})
    with pytest.raises(ScaffoldValidationError, match="expected object, got list"):
        _validated_prelecture_response({"prelecture": []})
    with pytest.raises(ScaffoldValidationError, match="prelecture variables: expected object, got null"):
        _validated_prelecture_response({"prelecture": {**fields, "variables": None}})
    typed = {"content": {**empty_content("concept"), "key_terms": ["Assets"]}, "evidence": []}
    assert _validated_content_response(typed, "concept") == typed
    with pytest.raises(ScaffoldValidationError, match="concept content: invalid fields"):
        _validated_content_response({"content": {**typed["content"], "steps": []}, "evidence": []}, "concept")


def test_enrichment_merges_case_variants_from_separate_slide_batches(tmp_path: Path) -> None:
    class VariantModel(MockModel):
        async def generate(self, stage: str, payload: dict) -> dict:
            if stage == "enriching_nodes":
                self.calls.append((stage, payload))
                term = "Go-Back-N" if payload["slides"][0]["page"] == 1 else "go-Back-N"
                return {"content": {**empty_content("concept"), "key_terms": [term]}, "evidence": []}
            return await super().generate(stage, payload)

    scaffold = run(tmp_path, VariantModel(), 4)
    assert scaffold["nodes"]["KN_0002"]["content"]["key_terms"] == ["Go-Back-N"]


def test_successful_model_calls_are_reused_for_unchanged_input(tmp_path: Path) -> None:
    workspace(tmp_path, 4)
    first = MockModel()
    asyncio.run(PreAgentRun(tmp_path, "lecture-1", "Accounting 101", first).run())
    assert first.calls
    second = MockModel(fail_stage="understanding_slides")
    asyncio.run(PreAgentRun(tmp_path, "lecture-1", "Accounting 101", second).run())
    assert second.calls == []
    assert json.loads((tmp_path / "runtime/preagent_state.json").read_text())["status"] == "completed"


def test_parsing_failure_is_a_real_failed_stage(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="normalized slides are missing"):
        asyncio.run(PreAgentRun(tmp_path, "lecture-1", "Accounting", MockModel()).run())
    state = json.loads((tmp_path / "runtime/preagent_state.json").read_text())
    assert state["current_stage"] == "parsing_slides"
    assert state["stages"]["parsing_slides"]["status"] == "failed"
    assert state["stages"]["parsing_slides"]["attempts"] == 1


def test_manifest_detects_tampering(tmp_path: Path) -> None:
    run(tmp_path, MockModel(), 4)
    (tmp_path / "prepared/lecture_scaffold.md").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="artifact hash mismatch"):
        resolve_committed_package(tmp_path)


def test_visual_slide_image_has_bounded_multimodal_input(tmp_path: Path) -> None:
    import types
    workspace(tmp_path, 1)
    path = tmp_path / "runtime/slide_images/slide_001.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (2).to_bytes(4, "big") + (2).to_bytes(4, "big") + b"a" * 80)
    normalized = tmp_path / "normalized/normalized_slides.json"
    value = json.loads(normalized.read_text())
    value["slides"][0]["vision"] = {"required": True, "slide_image_path": "runtime/slide_images/slide_001.png"}
    normalized.write_text(json.dumps(value))
    model = MockModel()
    asyncio.run(PreAgentRun(tmp_path, "lecture-1", "Accounting", model).run())
    assert any(stage == "understanding_slides" and payload["slides"][0]["image_path"] == "runtime/slide_images/slide_001.png" for stage, payload in model.calls)

    class FakeClient:
        def __init__(self) -> None:
            self.request = None
        async def create_chat_completion(self, **kwargs):
            self.request = kwargs
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content='{"descriptors":[]}'))])
    client = FakeClient()
    qwen = QwenScaffoldModel("mock", client, workspace_root=tmp_path)
    asyncio.run(qwen.generate("understanding_slides", {"slides": [{"page": 1, "text": "visual", "image_path": "runtime/slide_images/slide_001.png", "image_bytes": 104, "image_pixels": 4}]}))
    assert client.request["messages"][1]["content"][-1]["type"] == "image_url"
    assert client.request["response_format"] == {"type": "json_object"}
    assert "formulas MUST be []" in client.request["messages"][0]["content"]
    asyncio.run(qwen.generate("understanding_slides", {"slides": [{"page": 1, "text": "visual"}], "validation_feedback": {"error": "descriptor formulas: expected list, got null"}}))
    assert "descriptor formulas: expected list, got null" in client.request["messages"][0]["content"]
    assert client.request["response_format"]["type"] == "json_schema"
    descriptor_schema = client.request["response_format"]["json_schema"]["schema"]["properties"]["descriptors"]["items"]
    assert descriptor_schema["properties"]["page"]["enum"] == [1]
    assert descriptor_schema["properties"]["formulas"]["type"] == "array"
    asyncio.run(qwen.generate("extracting_knowledge", {"slides": [{"page": 17, "slide_text": "visual"}]}))
    assert "suggested_type (one of topic, concept, relationship, comparison, procedure, formula)" in client.request["messages"][0]["content"]
    assert "source_pages (nonempty array of unique" in client.request["messages"][0]["content"]
    assert client.request["response_format"]["type"] == "json_schema"
    candidate_schema = client.request["response_format"]["json_schema"]["schema"]["properties"]["candidates"]["items"]
    assert candidate_schema["properties"]["source_pages"]["items"]["enum"] == [17]
    assert "max_tokens" not in client.request
    asyncio.run(qwen.generate("building_scaffold", {"descriptors": [], "candidates": [], "allowed_candidate_ids": ["KC_0001"]}))
    assert "candidate_ids must be a nonempty array copied verbatim" in client.request["messages"][0]["content"]
    assert client.request["response_format"]["json_schema"]["schema"]["properties"]["nodes"]["items"]["properties"]["candidate_ids"]["items"]["enum"] == ["KC_0001"]
    assert client.request["timeout"] == BUILD_SCAFFOLD_TIMEOUT_SECONDS
    asyncio.run(qwen.generate("enriching_nodes", {"node": {"node_id": "KN_0001", "title": "Accounting", "node_type": "concept"}, "slides": []}))
    assert "definition, assumptions, mechanism, key_points, key_terms" in client.request["messages"][0]["content"]
    assert "describe FSM behavior as mechanism" in client.request["messages"][0]["content"]
    content_schema = client.request["response_format"]["json_schema"]["schema"]["properties"]["content"]
    assert set(content_schema["properties"]) == set(empty_content("concept"))
    assert "steps" not in content_schema["properties"]
    with pytest.raises(ValueError, match="image"):
        bounded_batches([{"image_bytes": 2_000_001}], max_items=4, max_bytes=16000, max_image_bytes=2_000_000)


def test_provider_schema_rejection_falls_back_to_json_mode() -> None:
    import types

    class SchemaRejected(Exception):
        status_code = 400

    class Client:
        def __init__(self) -> None:
            self.formats = []

        async def create_chat_completion(self, **request):
            self.formats.append(request["response_format"]["type"])
            if len(self.formats) == 1:
                raise SchemaRejected("response_format json_schema unsupported")
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content='{"descriptors":[]}'))])

    client = Client()
    model = QwenScaffoldModel("mock", client)
    result = asyncio.run(model.generate("understanding_slides", {"slides": [{"page": 1, "text": "text"}]}))
    asyncio.run(model.generate("understanding_slides", {"slides": [{"page": 2, "text": "text"}]}))
    assert result == {"descriptors": []}
    assert client.formats == ["json_schema", "json_object", "json_object"]


def test_class_session_freezes_committed_run_and_hotword_vocabulary(tmp_path: Path) -> None:
    index = tmp_path / "courses/index.json"
    index.parent.mkdir()
    index.write_text(json.dumps({"schema_version": 1, "course_ids": []}), encoding="utf-8")
    catalog = WorkspaceCatalog(tmp_path)
    course = catalog.create_course(title="Accounting")
    lecture = catalog.create_lecture(course["id"], number="01", title="Balance Sheet")
    catalog.upload_material(course["id"], lecture["id"], filename="slides.pdf", content=b"%PDF-mock")
    root = catalog.lecture_root(course["id"], lecture["id"])
    workspace(root, 4)
    catalog.mark_preparing(course["id"], lecture["id"])
    asyncio.run(PreAgentRun(root, lecture["id"], lecture["title"], MockModel()).run())
    catalog.mark_prepared(course["id"], lecture["id"])
    snapshot = catalog.committed_preparation_snapshot(course["id"], lecture["id"])
    session = catalog.start_session(course["id"], lecture["id"], preparation_snapshot=snapshot)
    assert session["preparationRunId"] == snapshot["runId"]
    assert session["preparationSnapshot"] == snapshot
    assert session["hotwordVocabularySnapshot"]["Balance Sheet"] == 1
    assert "Assets" not in session["hotwordVocabularySnapshot"]
    assert catalog.get_lecture(course["id"], lecture["id"])["preparationStatus"] == "ready"


def test_workspace_api_serves_scaffold_and_starts_committed_session(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient
    from classroom_app import create_app
    from config import Settings

    index = tmp_path / "courses/index.json"
    index.parent.mkdir()
    index.write_text(json.dumps({"schema_version": 1, "course_ids": []}), encoding="utf-8")
    catalog = WorkspaceCatalog(tmp_path)
    course = catalog.create_course(title="Accounting")
    lecture = catalog.create_lecture(course["id"], number="01", title="Balance Sheet")
    catalog.upload_material(course["id"], lecture["id"], filename="slides.pdf", content=b"%PDF-mock")
    root = catalog.lecture_root(course["id"], lecture["id"])
    workspace(root, 4)
    catalog.mark_preparing(course["id"], lecture["id"])
    asyncio.run(PreAgentRun(root, lecture["id"], lecture["title"], MockModel()).run())
    catalog.mark_prepared(course["id"], lecture["id"])
    app = create_app(Settings(api_key="test-key", workspace_id=None, send_workspace_header=False, source_language="en", target_language="zh", debug=False, raw_event_debug=False, agent1_enabled=False, agent1_output_path=tmp_path / "data"), workspace_catalog=catalog)
    client = TestClient(app)
    url = f"/api/courses/{course['id']}/lectures/{lecture['id']}"
    assert client.get(url + "/files/scaffold").json()["root_nodes"] == ["KN_0001"]
    session = client.post(url + "/sessions")
    assert session.status_code == 201
    assert session.json()["hotwordVocabularySnapshot"]["Balance Sheet"] == 1


@pytest.mark.parametrize("node_type,content", [
    ("concept", {"definition": "An idea.", "assumptions": ["No bit errors"], "mechanism": ["Sender sends a packet"], "key_points": [], "key_terms": ["ACK"]}),
    ("procedure", {"objective": "Detect errors.", "conditions": [], "steps": [{"order": 1, "content": "Compute the checksum."}], "result": None}),
    ("formula", {"expression": "U=L/R", "variables": [{"symbol": "L", "meaning": "Packet length"}], "interpretation": None, "assumptions": []}),
    ("comparison", {"items": ["TCP", "UDP"], "dimensions": [{"dimension": "Delivery", "values": {"TCP": "Reliable", "UDP": "Best effort"}}], "similarities": [], "differences": ["Reliability differs"]}),
])
def test_typed_content_accepts_only_its_shape(node_type: str, content: dict) -> None:
    validate_content(node_type, content)
    validate_content(node_type, empty_content(node_type))
    with pytest.raises(ScaffoldValidationError, match="invalid fields"):
        validate_content(node_type, {**content, "forbidden_cross_type_field": []})


def test_cross_type_fields_are_rejected() -> None:
    for node_type, field, value in [
        ("concept", "steps", []),
        ("procedure", "variables", []),
        ("formula", "steps", []),
        ("comparison", "expression", "x"),
    ]:
        with pytest.raises(ScaffoldValidationError, match="invalid fields"):
            validate_content(node_type, {**empty_content(node_type), field: value})


def test_enrichment_normalizes_only_absent_and_duplicate_values() -> None:
    response = _validated_content_response({"content": {
        "definition": "  Reliable transfer  ",
        "assumptions": None,
        "mechanism": [" send packet ", "send packet", " ", "Receive packet"],
        "key_terms": ["ACK", "ack"],
    }}, "concept")
    assert response == {"content": {"definition": "Reliable transfer", "assumptions": [],
        "mechanism": ["send packet", "Receive packet"], "key_points": [], "key_terms": ["ACK"]}, "evidence": []}
    assert compact_typed_content("formula", {"expression": " ", "variables": None}) == empty_content("formula")
    with pytest.raises(ScaffoldValidationError, match="invalid fields"):
        _validated_content_response({"content": {"definition": "X", "steps": []}}, "concept")
    with pytest.raises(ScaffoldValidationError, match="expected bounded list"):
        _validated_content_response({"content": {"steps": "first"}}, "procedure")
    evidence = _validated_content_response({"content": {**empty_content("concept"), "definition": "TCP provides reliable delivery."}, "evidence": [
        {"kind": "comparison", "label": None, "text": "TCP provides reliable delivery."},
        {"kind": "slide_example", "label": None, "text": "Slide diagram."},
        "Additional slide note.",
        {"kind": "other", "content": "Another slide note."},
        None,
    ]}, "concept")["evidence"]
    assert [item["kind"] for item in evidence] == ["slide_note", "slide_example", "slide_note", "slide_note"]
    assert evidence[0]["label"] == "comparison"


def test_formula_variables_keep_case_distinct_symbols() -> None:
    variables = [
        {"symbol": "F", "meaning": "Future worth"},
        {"symbol": "f", "meaning": "Geometric gradient rate"},
    ]
    response = _validated_content_response({"content": {
        "expression": "F = A_1[(1+i)^N - (1+f)^N]/(i-f)",
        "variables": [*variables, variables[0]],
    }}, "formula")
    assert response["content"]["variables"] == variables

    first = compact_typed_content("formula", {"expression": "F = P(1+i)^N", "variables": [variables[0]]})
    second = compact_typed_content("formula", {"variables": [variables[1]]})
    assert merge_typed_content("formula", [first, second])["variables"] == variables

    with pytest.raises(ScaffoldValidationError, match="duplicate symbol"):
        validate_content("formula", {**first, "variables": [variables[0], variables[0]]})


def test_worker_type_shapes_are_compacted_without_changing_canonical_schema() -> None:
    procedure = _validated_content_response({"content": {"steps": [
        "Read segment", {"order": 0, "content": "  Compute checksum  "},
        {"order": 7, "content": "Compute checksum"},
    ]}}, "procedure")["content"]
    assert procedure["steps"] == [{"order": 1, "content": "Read segment"}, {"order": 2, "content": "Compute checksum"}]

    formula = _validated_content_response({"content": {"expression": " U=L/R ", "variables": {"L": "Packet length", "R": "Link rate"}}}, "formula")["content"]
    assert formula["expression"] == "U=L/R"
    assert formula["variables"] == [{"symbol": "L", "meaning": "Packet length"}, {"symbol": "R", "meaning": "Link rate"}]

    comparison = _validated_content_response({"content": {"dimensions": [
        {"dimension": "Delivery", "values": [{"item": "TCP", "value": "Reliable"}]},
        {"dimension": "Delivery", "values": {"UDP": ["Best effort", "No ordering guarantee"]}},
        {"dimension": "Empty", "values": None},
    ]}}, "comparison")["content"]
    assert comparison["dimensions"] == [{"dimension": "Delivery", "values": {
        "TCP": "Reliable", "UDP": "Best effort; No ordering guarantee",
    }}]
    comparison = _validated_content_response({"content": {
        "items": [{"item": "TCP", "value": "Reliable, ordered delivery"},
                  {"item": "UDP", "value": "Connectionless delivery"}],
        "dimensions": [{"dimension": "Reliability", "values": [
            {"item": "TCP", "value": "Reliable"}, {"item": "UDP", "value": "Unreliable"},
        ]}],
    }}, "comparison")["content"]
    assert comparison["items"] == ["TCP", "UDP"]
    assert comparison["dimensions"] == [
        {"dimension": "Reliability", "values": {"TCP": "Reliable", "UDP": "Unreliable"}},
        {"dimension": "Overview", "values": {"TCP": "Reliable, ordered delivery", "UDP": "Connectionless delivery"}},
    ]
    with pytest.raises(ScaffoldValidationError, match="conflicting values"):
        _validated_content_response({"content": {"dimensions": [
            {"dimension": "Delivery", "values": {"TCP": "Reliable"}},
            {"dimension": "Delivery", "values": {"TCP": "Unreliable"}},
        ]}}, "comparison")


@pytest.mark.parametrize("node_type", ["topic", "concept", "procedure", "formula", "relationship"])
def test_provider_content_schema_matches_runtime_type_contract(node_type: str) -> None:
    response = _response_format("enriching_nodes", {"node": {"node_type": node_type}, "slides": []})
    content_schema = response["json_schema"]["schema"]["properties"]["content"]
    assert response["type"] == "json_schema"
    assert content_schema["additionalProperties"] is False
    assert set(content_schema["required"]) == set(empty_content(node_type))
    assert set(content_schema["properties"]) == set(empty_content(node_type))
    assert _validated_content_response({"content": empty_content(node_type), "evidence": []}, node_type)["content"] == empty_content(node_type)
    assert _response_format("enriching_nodes", {"node": {"node_type": node_type}, "slides": [{"image_path": "slide.png"}]}) == {"type": "json_object"}


def test_comparison_wire_values_are_normalized_to_canonical_map() -> None:
    wire = {"content": {"items": ["TCP", "UDP"], "dimensions": [
        {"dimension": "Reliability", "values": [{"item": "TCP", "value": "Reliable"}, {"item": "UDP", "value": "Best effort"}]},
        {"dimension": "Unsupported", "values": []},
    ], "similarities": [], "differences": []}, "evidence": []}
    normalized = _validated_content_response(wire, "comparison")
    assert normalized["content"]["dimensions"] == [{"dimension": "Reliability", "values": {"TCP": "Reliable", "UDP": "Best effort"}}]
    response = _response_format("enriching_nodes", {"node": {"node_type": "comparison"}, "slides": []})
    values = response["json_schema"]["schema"]["properties"]["content"]["properties"]["dimensions"]["items"]["properties"]["values"]
    assert values["type"] == "array"
    assert set(values["items"]["required"]) == {"item", "value"}
    assert _response_format("enriching_nodes", {"node": {"node_type": "comparison"}, "slides": [{"image_path": "slide.png"}]}) == {"type": "json_object"}
    with pytest.raises(ScaffoldValidationError, match="invalid values"):
        _validated_content_response({"content": {"dimensions": [{"dimension": "Reliability", "values": "TCP is reliable"}]}}, "comparison")


def test_comparison_list_values_commit_without_worker_retry(tmp_path: Path) -> None:
    class ComparisonModel(MockModel):
        async def generate(self, stage: str, payload: dict) -> dict:
            if stage == "enriching_nodes":
                self.calls.append((stage, payload))
                return {"content": {"items": ["TCP", "UDP"], "dimensions": [
                    {"dimension": "Delivery", "values": [{"item": "TCP", "value": "Reliable"}, {"item": "UDP", "value": "Best effort"}]},
                ], "similarities": [], "differences": []}, "evidence": [{"kind": "comparison", "label": None, "text": "A slide-supported contrast."}]}
            response = await super().generate(stage, payload)
            if stage == "building_scaffold":
                response["nodes"][1]["node_type"] = "comparison"
            return response

    model = ComparisonModel()
    scaffold = run(tmp_path, model, 2)
    assert scaffold["nodes"]["KN_0002"]["content"]["dimensions"][0]["values"] == {"TCP": "Reliable", "UDP": "Best effort"}
    assert scaffold["nodes"]["KN_0002"]["evidence"][0]["kind"] == "slide_note"
    assert len([stage for stage, _ in model.calls if stage == "enriching_nodes"]) == 1
    resolve_committed_package(tmp_path)


def test_relation_validation_and_deduplication(tmp_path: Path) -> None:
    scaffold = run(tmp_path, MockModel(), 2)
    edge = {"from_node_id": "KN_0001", "to_node_id": "KN_0002", "type": "applies_to", "reason": "The balance sheet belongs to the accounting topic.", "source_refs": [{"type": "slide", "page": 1}]}
    scaffold["relations"] = normalize_relations([edge, edge])
    assert len(scaffold["relations"]) == 1
    validate_scaffold(scaffold)
    missing = deepcopy(scaffold)
    missing["relations"][0]["to_node_id"] = "KN_9999"
    with pytest.raises(ScaffoldValidationError, match="missing target"):
        validate_scaffold(missing)
    self_edge = deepcopy(scaffold)
    self_edge["relations"][0]["to_node_id"] = "KN_0001"
    with pytest.raises(ScaffoldValidationError, match="self reference"):
        validate_scaffold(self_edge)
    duplicate = deepcopy(scaffold)
    duplicate["relations"].append({**edge, "relation_id": "REL-0002"})
    with pytest.raises(ScaffoldValidationError, match="duplicate edge"):
        validate_scaffold(duplicate)


def test_builder_relations_are_materialized_separately_from_tree(tmp_path: Path) -> None:
    class RelatingModel(MockModel):
        async def generate(self, stage: str, payload: dict) -> dict:
            response = await super().generate(stage, payload)
            if stage == "building_scaffold":
                relation = {"from_key": "accounting", "to_key": "balance", "type": "related_to",
                    "reason": "The slide discusses the balance sheet within accounting.", "source_pages": [1, 1]}
                response["relations"] = [relation, dict(relation)]
            return response

    scaffold = run(tmp_path, RelatingModel(), 2)
    assert scaffold["root_nodes"] == ["KN_0001"]
    assert scaffold["nodes"]["KN_0001"]["children"] == ["KN_0002"]
    assert scaffold["relations"] == []  # direct parent-child containment is already in the tree


def test_unsupported_builder_relation_does_not_retry_whole_lecture(tmp_path: Path) -> None:
    class OneBadRelationModel(MockModel):
        async def generate(self, stage: str, payload: dict) -> dict:
            response = await super().generate(stage, payload)
            if stage == "building_scaffold":
                response["relations"] = [
                    {"from_key": "accounting", "to_key": "balance", "type": "related_to",
                     "reason": "Both concepts appear on the supplied slide.", "source_pages": [1]},
                    {"from_key": "balance", "to_key": "accounting", "type": "related_to",
                     "reason": "This claim cites a page outside either node's evidence.", "source_pages": [99]},
                ]
            return response

    model = OneBadRelationModel()
    scaffold = run(tmp_path, model, 2)
    assert scaffold["relations"] == []
    assert [stage for stage, _ in model.calls].count("building_scaffold") == 1
    state = json.loads((tmp_path / "runtime/preagent_state.json").read_text())
    assert state["stages"]["building_scaffold"]["dropped_relations"] == 1


def test_transport_layer_v1_migration_preserves_typed_knowledge_and_legacy_evidence() -> None:
    path = Path(__file__).resolve().parents[1] / "courses/open-system/lectures/lecture-03-transport-layer/prepared/lecture_scaffold.json"
    if not path.is_file():
        pytest.skip("The user removed the old Transport layer preparation before rerunning")
    original = json.loads(path.read_text(encoding="utf-8"))
    if original["schema_version"] != 1:
        pytest.skip("The current user preparation is v2; this test needs the former v1 fixture")
    assert resolve_committed_package(path.parent.parent)["run_id"]
    assert original["schema_version"] == 1
    migrated = normalize_scaffold(original)
    validate_scaffold(migrated)
    assert migrated["schema_version"] == "2.0"
    assert migrated["relations"] == []
    assert len(migrated["nodes"]) == len(original["nodes"])
    rdt1 = migrated["nodes"]["KN_0005"]
    assert "perfectly reliable" in rdt1["scope"]
    assert rdt1["content"]["definition"] == original["nodes"]["KN_0005"]["prelecture"]["definition"]
    assert rdt1["content"]["assumptions"] == ["No bit errors", "No packet loss"]
    assert rdt1["content"]["mechanism"] == original["nodes"]["KN_0005"]["prelecture"]["steps"]
    assert "steps" not in rdt1["content"]
    assert all(item["kind"] == "slide_example" for item in rdt1["evidence"])
    procedure = migrated["nodes"]["KN_0003"]
    assert procedure["content"]["steps"][0] == {"order": 1, "content": original["nodes"]["KN_0003"]["prelecture"]["steps"][0]}
    formula = migrated["nodes"]["KN_0010"]
    assert formula["content"]["expression"] == original["nodes"]["KN_0010"]["prelecture"]["formula"]
    assert formula["content"]["variables"]
    assert any(item["kind"] == "legacy_comparison" for item in migrated["nodes"]["KN_0013"]["evidence"])


def test_v1_relation_migration_uses_existing_edges_without_guessing(tmp_path: Path) -> None:
    v2 = run(tmp_path, MockModel(), 2)
    legacy = {
        "schema_version": 1,
        "lecture_id": v2["lecture_id"], "title": v2["title"], "source": v2["source"],
        "root_nodes": v2["root_nodes"], "metadata": v2["metadata"],
        "nodes": {},
    }
    for node_id, node in v2["nodes"].items():
        legacy["nodes"][node_id] = {key: node[key] for key in ("node_id", "title", "node_type", "parent_id", "children", "order", "aliases", "source_refs")}
        legacy["nodes"][node_id]["prelecture"] = {**empty_prelecture(), "core_idea": node["scope"]}
        legacy["nodes"][node_id]["relations"] = {"prerequisites": [], "related_to": [], "contrasts_with": []}
    legacy["nodes"]["KN_0002"]["relations"]["prerequisites"] = ["KN_0001"]
    migrated = normalize_scaffold(legacy)
    assert migrated["relations"][0]["type"] == "depends_on"
    assert migrated["relations"][0]["source_refs"] == []
    assert "rationale unavailable" in migrated["relations"][0]["reason"]


def test_curated_transport_relations_require_ppt_evidence_and_do_not_change_tree() -> None:
    path = Path(__file__).resolve().parents[1] / "courses/open-system/lectures/lecture-03-transport-layer/prepared/lecture_scaffold.json"
    if not path.is_file():
        pytest.skip("The user removed the old Transport layer preparation before rerunning")
    original = json.loads(path.read_text(encoding="utf-8"))
    if original["schema_version"] != 1:
        pytest.skip("The current user preparation is v2; this test needs the former v1 fixture")
    migrated = normalize_scaffold(original)
    roots_before = deepcopy(migrated["root_nodes"])
    edges = [
        ("KN_0005", "KN_0006", "evolves_to", "RDT 1.0 assumes no bit errors; RDT 2.0 adds checksum and ACK/NAK to handle bit errors.", [15, 17]),
        ("KN_0006", "KN_0007", "evolves_to", "RDT 2.1 adds sequence numbers to handle corrupted ACK/NAK in RDT 2.0.", [20, 21]),
        ("KN_0007", "KN_0008", "evolves_to", "RDT 2.2 retains RDT 2.1 functionality without NAKs, using duplicate ACKs.", [23, 25]),
        ("KN_0012", "KN_0013", "contrasts_with", "SR buffers out-of-order packets and retransmits individual losses; GBN discards out-of-order packets and retransmits the window.", [42, 43]),
    ]
    migrated["relations"] = normalize_relations([{"from_node_id": source, "to_node_id": target, "type": kind, "reason": reason, "source_refs": [{"type": "slide", "page": page} for page in pages]} for source, target, kind, reason, pages in edges])
    validate_scaffold(migrated)
    assert migrated["root_nodes"] == roots_before
    assert [edge["type"] for edge in migrated["relations"]] == ["evolves_to", "evolves_to", "evolves_to", "contrasts_with"]
    assert migrated["nodes"]["KN_0002"]["title"] == "TCP and UDP Service Models"
    assert not any(edge["from_node_id"] == "KN_0002" and edge["to_node_id"] == "KN_0002" for edge in migrated["relations"])
