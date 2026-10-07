"""Offline checks for edit isolation and evidence matching; no real providers."""
import asyncio
import copy
import json

import pytest

from agent2_asr import _evidence_supports, apply_corrections, resolve_corrections, validate_corrections
from agent2_retrieval import Agent2Error, read_jsonl
from test_agent2_asr import FakeProvider, edit, runner


def evidence_context(tmp_path):
    run = runner(tmp_path)
    run.prepare()
    selected = run.evidence.selected_evidence({"node_matches": [], "page_matches": [{"page": 1, "reason": "match"}], "unmatched_reason": None})
    return run, dict(block_id="B001", units=run.units[:2], evidence=selected, page_ids={1, 2, 3}, expanded=False)


def response(edits):
    return {"block_id": "B001", "edits": edits, "additional_pages": []}


def test_valid_edit_survives_unsupported_neighbor(tmp_path):
    run, context = evidence_context(tmp_path)
    value = response([edit(), edit(unit_id="U0002", original="discount", replacement="capitalize", evidence=[])])
    before = copy.deepcopy(context["units"])
    issues = resolve_corrections(value, **context)
    assert [e["decision"] for e in value["edits"]] == ["apply", "flag"]
    corrected = apply_corrections(context["units"], value["edits"])
    assert corrected[0]["text"] == "We discuss Net Present Value."
    assert corrected[1] == before[1] and context["units"] == before
    assert issues[0]["unit_id"] == "U0002" and issues[0]["edit_index"] == 2
    assert "B001, edit 2, U0002" in issues[0]["error_message"]
    validate_corrections(value, **context)


@pytest.mark.parametrize("change", [
    {"unit_id": "U9999"}, {"original": "invented original"}, {"occurrence": 2},
    {"original": ""}, {"occurrence": True},
])
def test_unlocatable_proposal_is_audited_without_dropping_original_units(tmp_path, change):
    run, context = evidence_context(tmp_path)
    value = response([edit(**change)])
    before = copy.deepcopy(context["units"])
    issues = resolve_corrections(value, **context)
    assert value["edits"] == [] and issues[0]["action"] == "skipped"
    assert issues[0]["proposal"] == edit(**change)
    assert apply_corrections(context["units"], value["edits"]) == before


def test_bad_page_id_is_not_silently_repaired(tmp_path):
    run, context = evidence_context(tmp_path)
    value = response([edit(evidence=[{"page": 2, "evidence_id": "P001-E001"}]), edit(unit_id="U0002", original="discount", replacement="discounts")])
    issues = resolve_corrections(value, **context)
    assert value["edits"][0]["decision"] == "flag"
    assert value["edits"][0]["evidence"] == []
    assert value["edits"][1]["decision"] == "apply"
    assert "Unknown supplied evidence reference" in issues[0]["error_message"]


def test_malformed_flag_and_unknown_kind_can_be_preserved_safely(tmp_path):
    run, context = evidence_context(tmp_path)
    item = edit(decision="flag", replacement="", kind=[])
    item.pop("alignment")
    item.pop("evidence")
    value = response([item, "not an edit"])
    issues = resolve_corrections(value, **context)
    assert value["edits"][0]["replacement"] is None
    assert value["edits"][0]["decision"] == "flag"
    assert [i["action"] for i in issues] == ["flagged", "skipped"]
    validate_corrections(value, **context)


def test_duplicate_and_overlapping_proposals_never_apply_twice(tmp_path):
    run, context = evidence_context(tmp_path)
    value = response([edit(), edit()])
    issues = resolve_corrections(value, **context)
    assert [e["decision"] for e in value["edits"]] == ["apply", "flag"]
    assert "overlap" in issues[0]["error_message"]
    assert apply_corrections(context["units"], value["edits"])[0]["text"] == "We discuss Net Present Value."


@pytest.mark.parametrize("term,quote,supported", [
    ("RDT 2.0", "rdt2.0: reliable data transfer", True),
    ("Go Back N", "Go-Back-N", False),
    ("Go‑Back‑N", "Go-Back-N", True),
    ("Selective Repeat.", "Selective Repeat", True),
    ("timer", "timers", False),
    ("RDT 2.0", "rdt12.0", False),
    ("RDT 2.0", "rdt2.1", False),
    ("RDT 2", "rdt2.0", False),
    ("4.06%", "14.06%", False),
    ("10 ms", "10 s", False),
    ("base+N", "base-N", False),
    ("base N", "base-N", False),
    ("x y", "x-y", False),
    ("is_ACK", "isACK", False),
    ("no bit errors", "bit errors", False),
    ("acknowledgment", "acknowledgement", False),
])
def test_format_matching_does_not_equate_values_or_meanings(term, quote, supported):
    assert _evidence_supports(term, quote, "terminology") is supported


def test_wording_numeric_substring_is_not_evidence():
    assert not _evidence_supports("4.06%", "14.06%", "wording")


def test_version_format_can_apply_but_changed_version_cannot():
    units = [{"unit_id": "U0001", "text": "Use RDP 2.0."}]
    ev = {"pages": [{"page": 1, "text": "rdt2.0; rdt2.1"}]}
    context = dict(block_id="B001", units=units, evidence=ev, page_ids={1}, expanded=False)
    value = response([edit(original="RDP 2.0", replacement="RDT 2.0", evidence=[{"page": 1, "quote": "rdt2.0; rdt2.1"}])])
    assert resolve_corrections(value, **context) == []
    assert apply_corrections(units, value["edits"])[0]["text"] == "Use RDT 2.0."
    value = response([edit(original="RDP 2.0", replacement="RDT 2.1", evidence=[{"page": 1, "quote": "rdt2.0; rdt2.1"}])])
    issues = resolve_corrections(value, **context)
    assert value["edits"][0]["decision"] == "flag" and issues
    assert apply_corrections(units, value["edits"]) == units


def test_wrong_envelope_still_requires_repair(tmp_path):
    run, context = evidence_context(tmp_path)
    with pytest.raises(Agent2Error, match="B001"):
        resolve_corrections({"block_id": "B999", "edits": [], "additional_pages": []}, **context)


def test_recovery_audit_survives_cache_and_downstream_adapter(tmp_path):
    class MixedProvider(FakeProvider):
        async def create_chat_completion(self, **kwargs):
            raw = await super().create_chat_completion(**kwargs)
            if kwargs["messages"][0]["content"].startswith("You correct"):
                value = json.loads(raw)
                if value["block_id"] == "B001":
                    value["edits"] += [edit(unit_id="U0002", original="discount", replacement="capitalize", evidence=[]),
                                       edit(unit_id="U9999")]
                raw = json.dumps(value)
            return raw

    from agent2_enrichment_adapter import ASRAdapter
    provider = MixedProvider(fail_summary=True)
    run = runner(tmp_path, provider)
    originals = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert asyncio.run(run.run())["status"] == "completed_with_errors"
    provider.fail_summary = False
    status = asyncio.run(run.run())
    assert status["status"] == "completed"
    assert status["applied_edit_count"] == 1 and status["flagged_edit_count"] == 2
    assert status["validation_issue_count"] == 2 and status["skipped_edit_count"] == 1
    assert status["review_item_count"] == 3
    issues = read_jsonl(run.run_dir / "validation_issues.jsonl")
    assert [i["action"] for i in issues] == ["flagged", "skipped"]
    adapter = ASRAdapter(final_path=run.final_path, lecture_root=run.lecture_root, course_id="c", lecture_id="l", session_id="s")
    assert len(adapter.blocks[0]["validation_issues"]) == 2
    assert adapter.blocks[0]["units"][1]["text"] == "We discount future cash flows."
    assert all(p.read_bytes() == content for p, content in originals.items())
    assert provider.calls[("B001", "correction")] == 1
