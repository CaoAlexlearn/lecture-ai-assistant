"""Compact scaffold retrieval and page evidence, without reading the Markdown export."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from knowledge_scaffold import normalize_scaffold, validate_slide_descriptor

# Explicit roles prevent generated explanations from masquerading as slide
# quotations; consumed in model payloads, never added to the speech itself.
EVIDENCE_PROVENANCE = {
    "transcript_units": "recorded_speech_not_guaranteed_factual_truth",
    "nodes": "generated_scaffold_fallible_not_primary_evidence",
    "pages": "literal_extracted_page_text_not_observed_diagram",
    "evidence_conflict_policy": "preserve_and_flag_do_not_rewrite_speech",
}


class Agent2Error(ValueError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class LectureEvidence:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.scaffold = normalize_scaffold(json.loads(
            (root / "prepared/lecture_scaffold.json").read_text(encoding="utf-8")))
        document = json.loads((root / "normalized/normalized_slides.json").read_text(encoding="utf-8"))
        if document.get("lecture_id") != self.scaffold["lecture_id"]:
            raise Agent2Error("INVALID_EVIDENCE", "Scaffold and normalized slides belong to different lectures")
        slides = document.get("slides", [])
        if [slide.get("slide_number") for slide in slides] != list(range(1, len(slides) + 1)) or not slides:
            raise Agent2Error("INVALID_EVIDENCE", "Expected contiguous normalized slide pages")
        if len(slides) != self.scaffold["source"]["page_count"]:
            raise Agent2Error("INVALID_EVIDENCE", "Scaffold and normalized slide page counts differ")
        self.slides = {slide["slide_number"]: slide for slide in slides}
        self.page_segments = {page: self._page_segments(slide) for page, slide in self.slides.items()}
        self.page_text = {page: "\n".join(segment["text"] for segment in segments)
                          for page, segments in self.page_segments.items()}
        descriptors = read_jsonl(root / "intermediate/slide_descriptors.jsonl")
        for descriptor in descriptors:
            validate_slide_descriptor(descriptor, len(slides))
        if sorted(row["page"] for row in descriptors) != list(self.slides):
            raise Agent2Error("INVALID_EVIDENCE", "Page index must cover every slide exactly once")
        self.index = {"lecture_id": self.scaffold["lecture_id"],
                      "nodes": [self._index_node(node) for node in self.scaffold["nodes"].values()],
                      "pages": sorted(descriptors, key=lambda row: row["page"])}
        # Index, full nodes and page text all affect cached retrieval/correction.
        self.fingerprint = hashlib.sha256(json.dumps({"scaffold": self.scaffold,
            "document": document, "descriptors": descriptors}, sort_keys=True,
            ensure_ascii=False).encode()).hexdigest()

    @staticmethod
    def _page_segments(slide: dict[str, Any]) -> list[dict[str, str]]:
        pieces: list[str] = []
        for block in slide.get("blocks", []):
            if block.get("include_in_semantic_context") is False:
                continue
            for key in ("text", "latex", "table", "image_caption", "table_caption", "table_footnote"):
                value = block.get(key)
                if value:
                    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                    if text not in pieces:
                        pieces.append(text)
        return [{"evidence_id": f"P{slide['slide_number']:03d}-E{i:03d}", "text": text}
                for i, text in enumerate(pieces, 1)]

    def _index_node(self, node: dict[str, Any]) -> dict[str, Any]:
        path = []
        parent = node["parent_id"]
        while parent:
            ancestor = self.scaffold["nodes"][parent]
            path.insert(0, ancestor["title"])
            parent = ancestor["parent_id"]
        return {"node_id": node["node_id"], "title": node["title"],
                "node_type": node["node_type"], "aliases": node["aliases"],
                "scope": node["scope"], "path": path,
                "source_pages": [ref["page"] for ref in node["source_refs"]]}

    def validate_selection(self, value: Any) -> None:
        if not isinstance(value, dict) or set(value) != {"node_matches", "page_matches", "unmatched_reason"}:
            raise Agent2Error("INVALID_RETRIEVAL", "Expected node_matches, page_matches, unmatched_reason")
        for field, id_field, allowed in (("node_matches", "node_id", self.scaffold["nodes"]),
                                        ("page_matches", "page", self.slides)):
            rows = value[field]
            if not isinstance(rows, list):
                raise Agent2Error("INVALID_RETRIEVAL", f"{field} must be a list")
            seen = set()
            for row in rows:
                if not isinstance(row, dict) or set(row) != {id_field, "reason"}:
                    raise Agent2Error("INVALID_RETRIEVAL", f"Invalid {field} item")
                identifier = row[id_field]
                if (not isinstance(identifier, (str, int)) or isinstance(identifier, bool)
                        or identifier not in allowed or identifier in seen):
                    raise Agent2Error("INVALID_RETRIEVAL", f"Unknown or duplicate {id_field}: {identifier}")
                if not isinstance(row["reason"], str) or not row["reason"].strip():
                    raise Agent2Error("INVALID_RETRIEVAL", "Every candidate needs a matching reason")
                seen.add(identifier)
        reason = value["unmatched_reason"]
        if reason is not None and (not isinstance(reason, str) or not reason.strip()):
            raise Agent2Error("INVALID_RETRIEVAL", "unmatched_reason must be null or nonempty text")
        if not value["node_matches"] and not value["page_matches"] and not reason:
            raise Agent2Error("INVALID_RETRIEVAL", "An unmatched block needs an explanation")

    def selected_evidence(self, selection: dict[str, Any], extra_pages: list[int] = ()) -> dict[str, Any]:
        self.validate_selection(selection)
        nodes = [self.scaffold["nodes"][row["node_id"]] for row in selection["node_matches"]]
        pages = {row["page"] for row in selection["page_matches"]}
        pages.update(ref["page"] for node in nodes for ref in node["source_refs"])
        pages.update(extra_pages)
        if not pages <= self.slides.keys():
            raise Agent2Error("INVALID_RETRIEVAL", "Unknown additional slide page")
        return {"nodes": nodes, "pages": [{"page": page, "text": self.page_text[page],
            "segments": self.page_segments[page],
            "visual_required": bool(self.slides[page].get("vision", {}).get("required"))}
            for page in sorted(pages)]}

    def image_path(self, page: int) -> Path | None:
        vision = self.slides[page].get("vision", {})
        if not vision.get("required"):
            return None
        path = (self.root / vision["slide_image_path"]).resolve()
        path.relative_to(self.root)
        return path
