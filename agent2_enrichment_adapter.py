"""Read-only boundary between ASR correction and classroom enrichment.

Never reads translated text, Markdown, or uncorrected Agent1 summaries.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from agent1_v1 import _fingerprint, build_units, load_final_source, normalize_transcript
from agent2_retrieval import Agent2Error, LectureEvidence, read_jsonl


def require(condition, message):
    if not condition:
        raise Agent2Error("INVALID_UPSTREAM", message)


class ASRAdapter:
    CONTRACT = "corrected_blocks_v1"

    def __init__(self, *, final_path: Path, lecture_root: Path, course_id: str,
                 lecture_id: str, session_id: str):
        self.identity = dict(course_id=course_id, lecture_id=lecture_id, session_id=session_id)
        self.output = final_path.parent / "agent2"
        self.manifest = json.loads((self.output / "manifest.json").read_text(encoding="utf-8"))
        require(self.manifest.get("status") == "completed", "ASR must be completed, without failed blocks")
        require(all(self.manifest.get(k) == v for k, v in self.identity.items()), "ASR session identity mismatch")
        version = self.manifest.get("input_fingerprint")
        require(isinstance(version, str) and re.fullmatch(r"[0-9a-f]{64}", version), "Invalid ASR version")
        self.evidence = LectureEvidence(lecture_root)
        require(self.evidence.scaffold["lecture_id"] == lecture_id, "Wrong lecture scaffold")
        require(self.manifest.get("scaffold_fingerprint") == self.evidence.fingerprint,
                "ASR used a different scaffold or slide version; rerun upstream first")
        raw = read_jsonl(self.output / "runs" / version / "corrected_blocks.jsonl")
        require(bool(raw) and len(raw) == self.manifest.get("block_count") == self.manifest.get("completed_block_count")
                and not self.manifest.get("failed_block_ids"), "ASR completed block count is inconsistent")
        source = load_final_source(final_path, **self.identity)
        original = build_units(normalize_transcript(source))
        expected = {u["unit_id"]: u["text"] for u in original}
        seen_units, seen_blocks = [], set()
        self.blocks = []
        for row in raw:
            block_id = row.get("block_id")
            require(isinstance(block_id, str) and re.fullmatch(r"B\d+", block_id) and block_id not in seen_blocks,
                    "Invalid or duplicate ASR block ID")
            seen_blocks.add(block_id)
            before, after = row.get("original_units", []), row.get("corrected_units", [])
            require(bool(after) and [u["unit_id"] for u in before] == [u["unit_id"] for u in after],
                    f"{block_id}: corrected positions must match original units")
            require(all(expected.get(u["unit_id"]) == u["text"] for u in before),
                    f"{block_id}: ASR original text does not match current transcript")
            ids = [u["unit_id"] for u in after]
            require(ids[0] == row.get("start_unit_id") and ids[-1] == row.get("end_unit_id"), "Invalid block range")
            require(all(isinstance(u["text"], str) and u["text"].strip() for u in after), "Empty corrected unit")
            seen_units.extend(ids)
            selection = row.get("selection")
            if selection is None and (row.get("candidate_nodes") or row.get("selected_evidence")):
                saved = row.get("selected_evidence") or {}
                candidates = row.get("candidate_nodes") or saved.get("nodes", [])
                pages = row.get("evidence_pages", [p["page"] for p in saved.get("pages", [])])
                selection = {"node_matches": [{"node_id": n["node_id"], "reason": "Saved upstream ASR candidate"} for n in candidates],
                             "page_matches": [{"page": p, "reason": "Saved upstream ASR page evidence"} for p in pages],
                             "unmatched_reason": None}
            if selection is not None:
                self.evidence.validate_selection(selection)
                materials = self.evidence.selected_evidence(selection, row.get("additional_pages", []))
                persisted = row.get("selected_evidence")
                if persisted is not None:
                    # Full saved evidence is preferred only when it matches the bound source exactly.
                    require(_fingerprint(persisted) == _fingerprint(materials), "Persisted materials mismatch")
                    materials = persisted
                require(set(row.get("evidence_pages", [p["page"] for p in materials["pages"]])) == {p["page"] for p in materials["pages"]},
                        "ASR page evidence mismatch")
            else:
                materials = {"nodes": [], "pages": []}
            corrections = []
            for i, edit in enumerate(row.get("edits", [])):
                require(edit.get("unit_id") in ids and edit.get("decision") in {"apply", "flag"}, "Invalid correction reference")
                original_text = next(u["text"] for u in before if u["unit_id"] == edit["unit_id"])
                require(isinstance(edit.get("original"), str) and edit["original"] in original_text,
                        "Correction does not cite the original unit")
                for ref in edit.get("evidence", []):
                    require(type(ref.get("page")) is int and ref["page"] in self.evidence.page_text
                            and isinstance(ref.get("quote"), str) and ref["quote"]
                            and ref["quote"] in self.evidence.page_text[ref["page"]], "Invalid upstream correction page citation")
                corrections.append({**edit, "correction_id": f"{session_id}:{block_id}:edit:{i + 1}"})
            self.blocks.append({"block_id": block_id, "title": row.get("title", block_id),
                "start_unit_id": ids[0], "end_unit_id": ids[-1],
                "units": [{"unit_id": u["unit_id"], "text": u["text"],
                           "original_text": before[i]["text"]} for i, u in enumerate(after)],
                "summary": row.get("corrected_summary"), "corrections": corrections,
                "validation_issues": row.get("validation_issues", []),
                "materials": materials, "candidate_metadata_available": selection is not None})
        require(seen_units == list(expected), "ASR blocks must cover every original unit once, in order")
        self.version = {**self.identity, "asr_fingerprint": version,
                        "asr_artifact_fingerprint": _fingerprint(raw),
                        "scaffold_fingerprint": self.evidence.fingerprint,
                        "source_fingerprint": _fingerprint(original), "adapter_contract": self.CONTRACT}

    def index(self):
        return {"nodes": [{**n, "source_refs": self.evidence.scaffold["nodes"][n["node_id"]]["source_refs"]}
                          for n in self.evidence.index["nodes"]], "pages": self.evidence.index["pages"]}

    def sources(self, block, ranges):
        """Python character offsets in corrected units; exact quotes are resolved here, never invented by a model."""
        units = {u["unit_id"]: u for u in block["units"]}
        require(isinstance(ranges, list) and bool(ranges), "A fragment requires source ranges")
        refs, seen = [], set()
        for r in ranges:
            require(isinstance(r, dict) and set(r) == {"unit_id", "start", "end"}, "Invalid source range shape")
            unit = units.get(r["unit_id"])
            start, end = r["start"], r["end"]
            require(unit is not None and type(start) is int and type(end) is int
                    and 0 <= start < end <= len(unit["text"]),
                    f"Source range outside corrected unit {r['unit_id']}: received {start}:{end}; "
                    f"expected 0 <= start < end <= {len(unit['text']) if unit else 'unknown unit'}. "
                    "For a complete unit copy start=0 and end=char_length from target_units.")
            key = (r["unit_id"], start, end)
            require(key not in seen, "Duplicate source range")
            seen.add(key)
            refs.append({**r, **self.version, "block_id": block["block_id"],
                         "quote": unit["text"][start:end], "original_unit_text": unit["original_text"]})
        order = {u["unit_id"]: i for i, u in enumerate(block["units"])}
        return sorted(refs, key=lambda r: (order[r["unit_id"]], r["start"]))
