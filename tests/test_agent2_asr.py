from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent1_v1 import ModelSpec, build_units, normalize_transcript
from agent2_asr import Agent2ASRRun, apply_corrections, validate_corrections
from agent2_retrieval import Agent2Error, LectureEvidence, read_jsonl
from knowledge_scaffold import empty_content


def write(path: Path, value, *, lines=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in value) if lines else json.dumps(value), encoding="utf-8")


def fixture(root: Path, final: Path, course="c", lecture="l", session="s"):
    nodes = {}
    for i, (title, kind) in enumerate((("Net Present Value", "concept"), ("Discounting", "procedure")), 1):
        node_id = f"KN_{i:04d}"
        nodes[node_id] = {"node_id": node_id, "title": title, "node_type": kind,
            "parent_id": None, "children": [], "order": i - 1, "aliases": ["NPV"] if i == 1 else [],
            "scope": title + " for future cash flows.", "content": empty_content(kind), "evidence": [],
            "source_refs": [{"type": "slide", "page": 1}]}
    scaffold = {"schema_version": "2.0", "lecture_id": lecture, "title": "Finance",
        "source": {"slide_file": "source/original.pdf", "page_count": 3},
        "root_nodes": list(nodes), "nodes": nodes, "relations": [],
        "metadata": {"preagent_run_id": "test", "created_at": "today", "model": "fake", "prompt_version": "v2"}}
    write(root / "prepared/lecture_scaffold.json", scaffold)
    (root / "prepared/lecture_scaffold.md").write_text("UNTRUSTED READING EXPORT: never load this as evidence.")
    texts = ["Net Present Value discounts future cash flows.", "Ashea borrowed a loan at 4.06%.", "Discounting brings future cash flows to the present."]
    write(root / "normalized/normalized_slides.json", {"schema_version": "1.0", "lecture_id": lecture,
        "slides": [{"slide_number": i, "blocks": [{"text": text}], "vision": {"required": False}} for i, text in enumerate(texts, 1)]})
    write(root / "intermediate/slide_descriptors.jsonl", [{"page": i, "title": text,
        "topics": [text], "concepts": [], "formulas": [], "examples": [], "role": "concept_explanation"} for i, text in enumerate(texts, 1)], lines=True)
    transcript = "We discuss net prison value. We discount future cash flows. The rate is 4.16%. Now consolidate this loan."
    write(final, {"schema_version": 1, "status": "final", "course_id": course, "lecture_id": lecture,
        "session_id": session, "source": {"committed": transcript}})
    units = build_units(normalize_transcript(transcript))
    blocks = [{"block_id": "B001", "title": "Discounting", "start_unit_id": "U0001", "end_unit_id": "U0002"},
              {"block_id": "B002", "title": "Loan", "start_unit_id": "U0003", "end_unit_id": "U0004"}]
    directory = final.parent / "agent1"
    write(directory / "manifest.json", {"status": "completed"})
    write(directory / "segmentation.json", {"schema_version": "1.0", "blocks": blocks})
    write(directory / "transcript_units.jsonl", units, lines=True)
    write(directory / "blocks.jsonl", [{"block_id": block["block_id"], "title": block["title"],
        "summary": ["Uncorrected block."], "how_teacher_explained": [], "examples": [], "emphasis": []} for block in blocks], lines=True)
    return units, scaffold


def edit(**changes):
    return {"unit_id": "U0001", "original": "net prison value", "replacement": "Net Present Value",
        "occurrence": 1, "kind": "terminology", "decision": "apply", "reason": "NPV terminology and discounting context agree.",
        "evidence": [{"page": 1, "quote": "Net Present Value discounts future cash flows."}], "alignment": None, **changes}


class FakeProvider:
    def __init__(self, *, fail_summary=False, expand=False, invalid_retrieval=False):
        self.calls = Counter()
        self.fail_summary, self.expand, self.invalid_retrieval = fail_summary, expand, invalid_retrieval

    async def create_chat_completion(self, **kwargs):
        prompt = kwargs["messages"][0]["content"]
        payload = json.loads(kwargs["messages"][1]["content"])
        payload = payload.get("original_input", payload)
        block_id = payload["block_id"]
        stage = "retrieval" if prompt.startswith("You locate") else "correction" if prompt.startswith("You correct") else "summary"
        self.calls[(block_id, stage)] += 1
        await asyncio.sleep(0)
        if stage == "retrieval":
            assert all("content" not in row and "evidence" not in row for row in payload["indexes"]["nodes"])
            if self.invalid_retrieval and self.calls[(block_id, stage)] == 1:
                return json.dumps({"node_matches": [{"node_id": "KN_9999", "reason": "invented"}], "page_matches": [], "unmatched_reason": None})
            return json.dumps({"node_matches": [{"node_id": node_id, "reason": "Explains future cash flows."} for node_id in ("KN_0001", "KN_0002")] if block_id == "B001" else [],
                "page_matches": [{"page": 1 if block_id == "B001" else 2, "reason": "Same topic/example."}], "unmatched_reason": None})
        if stage == "correction":
            assert payload["selected_evidence"]["pages"]
            if self.expand and block_id == "B001" and not payload["evidence_expansion_used"]:
                return json.dumps({"block_id": block_id, "edits": [], "additional_pages": [3]})
            edits = [edit()] if block_id == "B001" else [edit(unit_id="U0003", original="4.16%", replacement=None,
                kind="number", decision="flag", reason="Different slide number; insufficient same-example evidence.",
                evidence=[{"page": 2, "quote": "Ashea borrowed a loan at 4.06%."}])]
            return json.dumps({"block_id": block_id, "edits": edits, "additional_pages": []})
        if self.fail_summary and block_id == "B002":
            return "{}"
        return json.dumps({"block_id": block_id, "title": payload["title"], "summary": [payload["transcript"]],
            "how_teacher_explained": [], "examples": [], "emphasis": []})


def runner(tmp_path, provider=None, *, limit=100000):
    root, final = tmp_path / "lecture", tmp_path / "data/realtime_final.json"
    fixture(root, final)
    return Agent2ASRRun(final_path=final, lecture_root=root, output_dir=final.parent / "agent2",
        course_id="c", lecture_id="l", session_id="s", provider=provider or FakeProvider(),
        spec=ModelSpec("fake", "fake", limit, 1024))


def test_many_to_many_retrieval_corrects_only_evidenced_spans_and_preserves_inputs(tmp_path):
    provider = FakeProvider(invalid_retrieval=True)
    run = runner(tmp_path, provider)
    originals = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    status = asyncio.run(run.run())
    assert status["status"] == "completed"
    assert status["applied_edit_count"] == status["flagged_edit_count"] == 1
    blocks = read_jsonl(run.run_dir / "corrected_blocks.jsonl")
    assert len(blocks[0]["candidate_nodes"]) == 2
    assert blocks[0]["evidence_pages"] == [1]  # Shared source page is read once.
    assert blocks[0]["corrected_units"][0]["text"] == "We discuss Net Present Value."
    assert "Net Present Value" in blocks[0]["corrected_summary"]["summary"][0]
    assert blocks[1]["corrected_units"] == blocks[1]["original_units"]
    assert all(p.read_bytes() == content for p, content in originals.items())
    before = provider.calls.copy()
    assert asyncio.run(run.run())["status"] == "completed"
    assert provider.calls == before  # Read/repeat does not issue new paid requests.


def test_expansion_reads_requested_page_and_retains_audit(tmp_path):
    run = runner(tmp_path, FakeProvider(expand=True))
    assert asyncio.run(run.run())["status"] == "completed"
    first = read_jsonl(run.run_dir / "corrected_blocks.jsonl")[0]
    assert first["evidence_pages"] == [1, 3]
    assert first["additional_pages"] == [3]
    assert (run.run_dir / "cache/B001/correction_expanded.json").is_file()


def test_retry_reuses_successful_correction_and_retries_failed_summary(tmp_path):
    provider = FakeProvider(fail_summary=True)
    run = runner(tmp_path, provider)
    first = asyncio.run(run.run())
    assert first["status"] == "completed_with_errors"
    assert first["failed_block_ids"] == ["B002"]
    before = provider.calls.copy()
    provider.fail_summary = False
    assert asyncio.run(run.run())["status"] == "completed"
    assert provider.calls[("B002", "summary")] == before[("B002", "summary")] + 1
    for key in before:
        if key != ("B002", "summary"):
            assert provider.calls[key] == before[key]


def test_changed_scaffold_creates_new_version_preserving_old_results(tmp_path):
    run = runner(tmp_path)
    assert asyncio.run(run.run())["status"] == "completed"
    old_path = run.run_dir / "corrected_blocks.jsonl"
    old_bytes = old_path.read_bytes()
    scaffold_path = run.lecture_root / "prepared/lecture_scaffold.json"
    value = json.loads(scaffold_path.read_text())
    value["nodes"]["KN_0001"]["scope"] += " Updated scope."
    write(scaffold_path, value)
    second = Agent2ASRRun(final_path=run.final_path, lecture_root=run.lecture_root,
        output_dir=run.output_dir, course_id="c", lecture_id="l", session_id="s",
        provider=FakeProvider(), spec=run.spec)
    assert asyncio.run(second.run())["status"] == "completed"
    assert second.run_dir != run.run_dir
    assert old_path.read_bytes() == old_bytes


def test_budget_failure_makes_no_provider_call(tmp_path):
    provider = FakeProvider()
    run = runner(tmp_path, provider, limit=100)
    status = asyncio.run(run.run())
    assert status["status"] == "failed"
    assert status["root_cause"]["error_type"] == "CONTEXT_LIMIT_EXCEEDED"
    assert not provider.calls


def test_qwen_stream_uses_bounded_nonthinking_requests_and_ignores_reasoning(tmp_path):
    class StreamProvider:
        async def create_chat_completion(self, **kwargs):
            assert kwargs["stream"] is True
            assert kwargs["extra_body"] == {"enable_thinking": False}
            assert kwargs["response_format"] == {"type": "json_object"}
            async def chunks():
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(reasoning_content="ignored", content=None), finish_reason=None)])
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='{"ok":true}'), finish_reason="stop")])
            return chunks()
    run = runner(tmp_path, StreamProvider())
    run.spec = ModelSpec("alibaba_cloud_model_studio", "fake", 100000, 1024)
    assert asyncio.run(run._call("retrieval", {})) == {"ok": True}


def test_transient_failure_retries_without_leaving_a_failed_completion(tmp_path, monkeypatch):
    import agent2_asr
    monkeypatch.setattr(agent2_asr, "_retry_delay", lambda _attempt: 0)
    class FlakyProvider(FakeProvider):
        attempts = 0
        async def create_chat_completion(self, **kwargs):
            self.attempts += 1
            if self.attempts == 1:
                raise ConnectionError("temporary outage")
            return await super().create_chat_completion(**kwargs)
    run = runner(tmp_path, FlakyProvider())
    status = asyncio.run(run.run())
    assert status["status"] == "completed"
    assert "root_cause" not in status
    assert "provider_retry" in (run.run_dir / "errors.jsonl").read_text()


def test_changed_final_source_is_rejected_before_model_calls(tmp_path):
    run = runner(tmp_path)
    value = json.loads(run.final_path.read_text())
    value["source"]["committed"] += " New content."
    write(run.final_path, value)
    with pytest.raises(Agent2Error, match="do not match"):
        run.prepare()
    assert not run.provider.calls


@pytest.mark.parametrize("change", [
    {"unit_id": "U9999"}, {"original": "nonexistent"}, {"occurrence": 2},
    {"evidence": [{"page": 1, "quote": "Invented evidence"}]},
    {"evidence": [{"page": 3, "quote": "Net Present Value discounts future cash flows."}]},
    {"replacement": "Invented terminology"},
])
def test_bad_source_or_citation_cannot_be_applied(tmp_path, change):
    run = runner(tmp_path)
    run.prepare()
    evidence = run.evidence.selected_evidence({"node_matches": [], "page_matches": [{"page": 1, "reason": "match"}], "unmatched_reason": None})
    with pytest.raises(Agent2Error):
        validate_corrections({"block_id": "B001", "edits": [edit(**change)], "additional_pages": []},
            block_id="B001", units=run.units[:2], evidence=evidence, page_ids={1, 2, 3}, expanded=False)


def test_numeric_relabeling_does_not_bypass_alignment_gate(tmp_path):
    run = runner(tmp_path)
    run.prepare()
    evidence = run.evidence.selected_evidence({"node_matches": [], "page_matches": [{"page": 2, "reason": "match"}], "unmatched_reason": None})
    item = edit(unit_id="U0003", original="4.16%", replacement="4.06%", kind="wording",
        evidence=[{"page": 2, "quote": "Ashea borrowed a loan at 4.06%."}])
    with pytest.raises(Agent2Error, match="alignment"):
        validate_corrections({"block_id": "B002", "edits": [item], "additional_pages": []},
            block_id="B002", units=run.units[2:], evidence=evidence, page_ids={1, 2, 3}, expanded=False)


def test_patch_offsets_preserve_other_occurrences_and_handle_length_changes():
    units = [{"unit_id": "U0001", "text": "wrong and wrong then bad."}]
    edits = [edit(original="wrong", replacement="correct term", occurrence=2), edit(original="bad", replacement="good")]
    assert apply_corrections(units, edits)[0]["text"] == "wrong and correct term then good."
    assert units[0]["text"] == "wrong and wrong then bad."


def test_high_impact_edit_requires_more_than_the_conflicting_values():
    units = [{"unit_id": "U0001", "text": "Ashea borrowed a loan at 4.16%."},
             {"unit_id": "U0002", "text": "For this loan use 4.06%."}]
    evidence = {"pages": [{"page": 2, "text": "Ashea borrowed a loan at 4.06%."}]}
    item = edit(original="4.16%", replacement="4.06%", kind="number",
        evidence=[{"page": 2, "quote": evidence["pages"][0]["text"]}],
        alignment={"page": 2, "transcript_quote": "4.16%", "slide_quote": "4.06%"})
    value = {"block_id": "B001", "edits": [item], "additional_pages": []}
    with pytest.raises(Agent2Error, match="context"):
        validate_corrections(value, block_id="B001", units=units, evidence=evidence, page_ids={2}, expanded=False)
    item["alignment"] = {"page": 2, "transcript_quote": units[0]["text"], "slide_quote": evidence["pages"][0]["text"]}
    validate_corrections(value, block_id="B001", units=units, evidence=evidence, page_ids={2}, expanded=False)
    assert item["decision"] == "flag"  # A second spoken value is not a correction.
    assert apply_corrections(units, [item]) == units
    units[1]["text"] = "Sorry, I mean 4.06% for this loan."
    item["decision"] = "apply"
    validate_corrections(value, block_id="B001", units=units, evidence=evidence, page_ids={2}, expanded=False)
    assert apply_corrections(units, [item])[0]["text"] == "Ashea borrowed a loan at 4.06%."


def test_stable_evidence_ids_are_resolved_to_exact_quotes(tmp_path):
    run = runner(tmp_path)
    run.prepare()
    selected = run.evidence.selected_evidence({"node_matches": [], "page_matches": [{"page": 1, "reason": "match"}], "unmatched_reason": None})
    item = edit(evidence=[{"page": 1, "evidence_id": "P001-E001"}])
    value = {"block_id": "B001", "edits": [item], "additional_pages": []}
    validate_corrections(value, block_id="B001", units=run.units[:2], evidence=selected, page_ids={1, 2, 3}, expanded=False)
    assert item["evidence"] == [{"page": 1, "quote": "Net Present Value discounts future cash flows."}]
    # Resolved cache output remains independently valid.
    validate_corrections(value, block_id="B001", units=run.units[:2], evidence=selected, page_ids={1, 2, 3}, expanded=False)
    item["evidence"] = [{"page": 1, "evidence_id": "P001-E999"}]
    with pytest.raises(Agent2Error, match="Unknown"):
        validate_corrections(value, block_id="B001", units=run.units[:2], evidence=selected, page_ids={1, 2, 3}, expanded=False)


def test_numeric_slide_difference_without_spoken_corroboration_is_flagged():
    units = [{"unit_id": "U0001", "text": "Ashea borrowed a loan at 4.16%."}]
    evidence = {"pages": [{"page": 2, "text": "Ashea borrowed a loan at 4.06%.",
        "segments": [{"evidence_id": "P002-E001", "text": "Ashea borrowed a loan at 4.06%."}]}]}
    item = edit(original="4.16%", replacement="4.06%", kind="number",
        evidence=[{"page": 2, "evidence_id": "P002-E001"}],
        alignment={"page": 2, "evidence_id": "P002-E001", "start_unit_id": "U0001", "end_unit_id": "U0001"})
    validate_corrections({"block_id": "B001", "edits": [item], "additional_pages": []},
        block_id="B001", units=units, evidence=evidence, page_ids={2}, expanded=False)
    assert item["decision"] == "flag"
    assert apply_corrections(units, [item]) == units


def test_overlapping_applied_edits_rejected(tmp_path):
    run = runner(tmp_path)
    run.prepare()
    evidence = {"pages": [{"page": 1, "text": "Net Present Value discounts future cash flows."}]}
    with pytest.raises(Agent2Error, match="overlap"):
        validate_corrections({"block_id": "B001", "edits": [edit(), edit()], "additional_pages": []},
            block_id="B001", units=run.units[:2], evidence=evidence, page_ids={1}, expanded=False)


def test_compact_index_is_separate_from_full_evidence_and_md(tmp_path):
    run = runner(tmp_path)
    evidence = LectureEvidence(run.lecture_root)
    assert all("content" not in node and "evidence" not in node for node in evidence.index["nodes"])
    assert evidence.index["nodes"][0]["source_pages"] == [1]
    with pytest.raises(Agent2Error):
        evidence.validate_selection({"node_matches": [], "page_matches": [{"page": 99, "reason": "made up"}], "unmatched_reason": None})


def test_routes_require_completed_agent1_and_show_saved_results(tmp_path, monkeypatch):
    import classroom_app
    from classroom_app import create_app
    from config import Settings
    from workspace_catalog import WorkspaceCatalog
    catalog = WorkspaceCatalog(tmp_path)
    write(tmp_path / "courses/index.json", {"schema_version": 1, "course_ids": []})
    course = catalog.create_course(title="Finance")
    lecture = catalog.create_lecture(course["id"], number="01", title="Discounting")
    catalog.upload_material(course["id"], lecture["id"], filename="slides.pdf", content=b"%PDF")
    root = catalog.lecture_root(course["id"], lecture["id"])
    prepared = root / "prepared"
    prepared.mkdir()
    for name in ("lecture_scaffold.json", "lecture_scaffold.md", "asr_context.txt", "hotwords.json"):
        (prepared / name).write_text("[]")
    catalog.mark_prepared(course["id"], lecture["id"])
    session = catalog.start_session(course["id"], lecture["id"])
    final = tmp_path / session["transcriptPath"]
    fixture(root, final, course["id"], lecture["id"], session["id"])
    fake = FakeProvider()
    monkeypatch.setattr(classroom_app, "provider_for_agent2", lambda _spec: fake)
    monkeypatch.setenv("AGENT2_CONTEXT_WINDOW_TOKENS", "100000")
    app = create_app(Settings(api_key="test", workspace_id=None, send_workspace_header=False,
        source_language="en", target_language="zh", debug=False, raw_event_debug=False), workspace_catalog=catalog)
    url = f"/api/courses/{course['id']}/lectures/{lecture['id']}/sessions/{session['id']}/agent2"
    with TestClient(app) as client:
        assert client.post(url + "/run").status_code == 400
        catalog.complete_session(course["id"], lecture["id"], session["id"])
        write(final.parent / "agent1/manifest.json", {"status": "failed"})
        assert client.get(url).json()["agent1_ready"] is False
        assert client.post(url + "/run").status_code == 400
        assert not fake.calls
        write(final.parent / "agent1/manifest.json", {"status": "completed"})
        assert client.post(url + "/run").status_code == 202
        for _ in range(100):
            status = client.get(url).json()
            if status["status"] != "processing":
                break
            time.sleep(0.01)
        assert status["status"] == "completed"
        data = client.get(url + "/results").json()
        assert len(data["blocks"]) == 2
        assert data["blocks"][0]["edits"][0]["decision"] == "apply"
        calls = fake.calls.copy()
        assert client.post(url + "/run").status_code == 202
        for _ in range(100):
            if client.get(url).json()["status"] != "processing":
                break
            time.sleep(0.01)
        assert fake.calls == calls
        assert client.get(url.replace(course["id"], "missing") + "/results").status_code in (400, 404)
