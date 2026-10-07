"""Production PreAgent: bounded slide work, one canonical scaffold, atomic commit."""

from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from knowledge_scaffold import (
    NODE_TYPES,
    PRELECTURE_FIELDS,
    RELATION_TYPES,
    ROLES,
    ScaffoldValidationError,
    canonical_json,
    compact_typed_content,
    compact_worker_evidence,
    compact_slide_descriptor,
    dedupe_casefold_strings,
    dedupe_candidates,
    derive_asr_context,
    derive_hotwords,
    empty_content,
    empty_prelecture,
    merge_typed_content,
    normalize_relations,
    render_scaffold_markdown,
    validate_knowledge_candidate,
    validate_content_population,
    validate_prelecture,
    validate_scaffold,
    validate_slide_descriptor,
    validate_relation,
)
from scaffold_cleanup import MAX_RELATIONS_PER_LECTURE, clean_relations, consolidate_nodes, supplement_explicit_relations
from slide_evidence import block_evidence_text, supplement_native_pdf_text

STATE = "runtime/preagent_state.json"
MANIFEST = "runtime/runs/{run_id}/manifest.json"
ARTIFACTS = ("intermediate/slide_descriptors.jsonl", "intermediate/knowledge_candidates.jsonl", "prepared/lecture_scaffold.json", "prepared/lecture_scaffold.md", "prepared/hotwords.json", "prepared/asr_context.txt")
STAGES = ("parsing_slides", "understanding_slides", "extracting_knowledge", "building_scaffold", "enriching_nodes", "covering_relations", "generating_derived_outputs")
PROMPT_VERSION = "v2"
MAX_ATTEMPTS = 2
CACHE_VERSION = 5
BUILD_SCAFFOLD_TIMEOUT_SECONDS = 240.0


class PreAgentError(RuntimeError):
    def __init__(self, stage: str, scope: str, message: str, attempts: int = 0) -> None:
        self.stage, self.scope, self.attempts = stage, scope, attempts
        super().__init__(f"{stage} [{scope}] failed after {attempts} attempt(s): {message}")


class ScaffoldModel(Protocol):
    model_name: str
    async def generate(self, stage: str, payload: dict[str, Any]) -> Any: ...


def _validated_prelecture_response(value: Any) -> dict[str, Any]:
    """Accept harmless response envelopes, then enforce the canonical fields."""
    if not isinstance(value, dict):
        raise ScaffoldValidationError(f"enrichment response: expected object, got {type(value).__name__}")
    if "prelecture" in value:
        prelecture = value["prelecture"]
        if not isinstance(prelecture, dict):
            actual = "null" if prelecture is None else type(prelecture).__name__
            raise ScaffoldValidationError(f"enrichment prelecture: expected object, got {actual}")
    elif set(value) == set(PRELECTURE_FIELDS):
        prelecture = value
    else:
        keys = sorted(str(key)[:50] for key in value)[:12]
        raise ScaffoldValidationError(
            f"enrichment response: expected prelecture object or direct prelecture fields; got keys {keys!r}"
        )
    validate_prelecture(prelecture)
    return prelecture


def _validated_content_response(value: Any, node_type: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not set(value) <= {"content", "evidence"} or "content" not in value:
        raise ScaffoldValidationError("enrichment response: expected content and evidence")
    content = compact_typed_content(node_type, value["content"])
    evidence = compact_worker_evidence(value.get("evidence"))
    validate_content_population(node_type, content, evidence)
    return {"content": content, "evidence": evidence}


_OPERATIONAL_TITLE = re.compile(r"\b(?:sender|receiver)\s+(?:fsm|logic|algorithm|workflow|rules?)\b|\b(?:calculation|computing|procedure|workflow)\b", re.I)
_PROTOCOL_OBJECT = re.compile(r"\brdt\s*\d+(?:\.\d+)+\b|\b(?:protocol|model|mechanism|architecture|principle)\b", re.I)
_STRUCTURAL_TITLE = re.compile(
    r"^(?:agenda|outline|table of contents|lecture roadmap|recap|summary|course topics summary|thank you|references|section divider|take.home exercise|exercise prompts?|discussion questions?)$|"
    r"^(?:course|lecture|chapter|section)\s+(?:topics?\s+)?(?:summary|recap|outline|roadmap)$",
    re.I,
)


def classify_node_type(title: str, scope: str, proposed: str) -> str:
    """Resolve protocol objects versus operational procedures before enrichment."""
    if proposed not in {"topic", "concept", "procedure"}:
        return proposed
    if _OPERATIONAL_TITLE.search(title):
        return "procedure"
    named_object = re.search(r"\brdt\s*\d+(?:\.\d+)+\b|\bprotocols?\b|\bmodels?\b|\bmechanisms?\b|\barchitectures?\b", title, re.I)
    if (proposed != "topic" and _PROTOCOL_OBJECT.search(title)) or named_object:
        return "concept"
    return proposed


def is_structural_candidate(title: str) -> bool:
    """A slide heading alone is not an independent knowledge unit."""
    return bool(_STRUCTURAL_TITLE.fullmatch(" ".join(title.split())))


def discard_unsupported_builder_relations(value: Any, candidates: Sequence[dict[str, Any]]) -> tuple[Any, int]:
    """Keep a valid tree when optional relations lack endpoint slide evidence.

    A relation with an invalid citation cannot be safely repaired by guessing a
    different page or rewriting its reason. The later coverage pass can inspect
    missing relations against the source slides.
    """
    if not isinstance(value, dict) or not isinstance(value.get("nodes"), list) or not isinstance(value.get("relations"), list):
        return value, 0
    candidate_pages = {item["candidate_id"]: set(item["source_pages"]) for item in candidates}
    node_pages: dict[str, set[int]] = {}
    for node in value["nodes"]:
        if not isinstance(node, dict) or not isinstance(node.get("key"), str) or not isinstance(node.get("candidate_ids"), list):
            continue
        refs = node["candidate_ids"]
        if all(isinstance(ref, str) and ref in candidate_pages for ref in refs):
            node_pages[node["key"]] = set().union(*(candidate_pages[ref] for ref in refs))
    retained = []
    for relation in value["relations"]:
        if not isinstance(relation, dict) or set(relation) != {"from_key", "to_key", "type", "reason", "source_pages"}:
            continue
        source, target, pages = relation["from_key"], relation["to_key"], relation["source_pages"]
        if (not isinstance(source, str) or not isinstance(target, str) or source == target
                or source not in node_pages or target not in node_pages
                or not isinstance(relation["type"], str) or relation["type"] not in RELATION_TYPES
                or not isinstance(relation["reason"], str) or not relation["reason"].strip()
                or len(relation["reason"]) > 240
                or not isinstance(pages, list) or not pages or any(type(page) is not int for page in pages)
                or not set(pages) <= node_pages[source] | node_pages[target]):
            continue
        retained.append({**relation, "source_pages": sorted(set(pages))})
    return {**value, "relations": retained}, len(value["relations"]) - len(retained)


def relation_coverage_candidates(scaffold: dict[str, Any]) -> list[dict[str, str]]:
    """Flag same-topic version transitions for evidence review, without creating edges."""
    groups: dict[tuple[str | None, str], list[tuple[tuple[int, ...], str]]] = {}
    for node in scaffold["nodes"].values():
        match = re.search(r"\b([a-z][a-z0-9-]*)\s*(\d+(?:\.\d+)+)\b", node["title"], re.I)
        if match and node["node_type"] == "concept":
            key = (node["parent_id"], match.group(1).casefold())
            groups.setdefault(key, []).append((tuple(map(int, match.group(2).split("."))), node["node_id"]))
    edges = {(r["from_node_id"], r["to_node_id"]) for r in scaffold["relations"]}
    result: list[dict[str, str]] = []
    for group in groups.values():
        ordered = [node_id for _, node_id in sorted(group)]
        for source, target in zip(ordered, ordered[1:]):
            if (source, target) in edges:
                continue
            result.append({"from_node_id": source, "to_node_id": target})
    return result


def validate_relation_coverage_response(value: Any, nodes: dict[str, dict[str, Any]], page_count: int) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != {"relations"} or not isinstance(value["relations"], list):
        raise ScaffoldValidationError("relation coverage: expected relations list")
    additions: list[dict[str, Any]] = []
    for index, raw in enumerate(value["relations"], 1):
        if not isinstance(raw, dict) or set(raw) != {"from_node_id", "to_node_id", "type", "reason", "source_pages"}:
            continue
        pages = raw["source_pages"]
        if not isinstance(pages, list) or not pages or any(type(page) is not int for page in pages):
            continue
        relation = {"relation_id": f"REL-{index:04d}", "from_node_id": raw["from_node_id"],
                    "to_node_id": raw["to_node_id"], "type": raw["type"], "reason": raw["reason"],
                    "source_refs": [{"type": "slide", "page": page} for page in sorted(set(pages))]}
        try:
            validate_relation(relation, nodes, page_count)
        except (ScaffoldValidationError, TypeError, KeyError):
            continue
        additions.append(relation)
    return additions


def _strict_object(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _content_schema(node_type: str) -> dict[str, Any]:
    string_array = {"type": "array", "items": {"type": "string"}}
    nullable_string = {"type": ["string", "null"]}
    shapes = {
        "topic": {"summary": nullable_string, "key_points": string_array},
        "concept": {"definition": nullable_string, "assumptions": string_array,
                    "mechanism": string_array, "key_points": string_array, "key_terms": string_array},
        "procedure": {"objective": nullable_string, "conditions": string_array,
                      "steps": {"type": "array", "items": _strict_object({"order": {"type": "integer"}, "content": {"type": "string"}})},
                      "rules": string_array,
                      "result": nullable_string},
        "formula": {"expression": nullable_string,
                    "variables": {"type": "array", "items": _strict_object({"symbol": {"type": "string"}, "meaning": {"type": "string"}})},
                    "interpretation": nullable_string, "assumptions": string_array},
        "comparison": {"items": string_array,
                       "dimensions": {"type": "array", "items": _strict_object({
                           "dimension": {"type": "string"},
                           "values": {"type": "array", "items": _strict_object({"item": {"type": "string"}, "value": {"type": "string"}})},
                       })},
                       "similarities": string_array, "differences": string_array},
        "relationship": {"statement": nullable_string, "key_points": string_array},
    }
    return _strict_object(shapes[node_type])


def _response_format(stage: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Apply provider schema constraints only to text-only tasks."""
    string_array = {"type": "array", "items": {"type": "string"}}
    if stage == "understanding_slides" and not any(item.get("image_path") for item in payload["slides"]):
        pages = [item["page"] for item in payload["slides"]]
        schema = _strict_object({"descriptors": {"type": "array", "items": _strict_object({
            "page": {"type": "integer", "enum": pages},
            "title": {"type": ["string", "null"]},
            "topics": string_array, "concepts": string_array, "formulas": string_array, "examples": string_array,
            "role": {"type": "string", "enum": sorted(ROLES)},
        })}})
    elif stage == "extracting_knowledge":
        pages = [item["page"] for item in payload["slides"]]
        schema = _strict_object({"candidates": {"type": "array", "items": _strict_object({
            "title": {"type": "string"},
            "suggested_type": {"type": "string", "enum": sorted(NODE_TYPES)},
            "aliases": string_array,
            "source_pages": {"type": "array", "items": {"type": "integer", "enum": pages}},
        })}})
    elif stage == "building_scaffold":
        ids = payload["allowed_candidate_ids"]
        schema = _strict_object({
            "nodes": {"type": "array", "items": _strict_object({
                "key": {"type": "string"},
                "title": {"type": "string"},
                "node_type": {"type": "string", "enum": sorted(NODE_TYPES)},
                "parent_key": {"type": ["string", "null"]},
                "candidate_ids": {"type": "array", "items": {"type": "string", "enum": ids}},
                "aliases": string_array,
                "scope": {"type": "string"},
            })},
            "relations": {"type": "array", "items": _strict_object({
                "from_key": {"type": "string"},
                "to_key": {"type": "string"},
                "type": {"type": "string", "enum": sorted(RELATION_TYPES)},
                "reason": {"type": "string"},
                "source_pages": {"type": "array", "items": {"type": "integer"}},
            })},
        })
    elif stage == "enriching_nodes" and not any(item.get("image_path") for item in payload["slides"]):
        schema = _strict_object({
            "content": _content_schema(payload["node"]["node_type"]),
            "evidence": {"type": "array", "items": _strict_object({
                "kind": {"type": "string", "enum": ["slide_example"]},
                "label": {"type": ["string", "null"]},
                "text": {"type": "string"},
            })},
        })
    elif stage == "covering_relations":
        ids = [item["node_id"] for item in payload["nodes"]]
        schema = _strict_object({"relations": {"type": "array", "items": _strict_object({
            "from_node_id": {"type": "string", "enum": ids},
            "to_node_id": {"type": "string", "enum": ids},
            "type": {"type": "string", "enum": sorted(RELATION_TYPES)},
            "reason": {"type": "string"},
            "source_pages": {"type": "array", "items": {"type": "integer"}},
        })}})
    else:
        return {"type": "json_object"}
    return {"type": "json_schema", "json_schema": {"name": f"preagent_{stage}", "strict": True, "schema": schema}}


class QwenScaffoldModel:
    """One bounded JSON call per task; no autonomous repair loop."""

    def __init__(self, model_name: str = "qwen3.8-max", client: Any | None = None, base_prompt: str = "", workspace_root: Path | None = None) -> None:
        self.model_name = model_name
        self.client = client
        self.base_prompt = base_prompt
        self.workspace_root = workspace_root.resolve() if workspace_root else None
        self._json_schema_supported = True

    async def generate(self, stage: str, payload: dict[str, Any]) -> Any:
        instructions = {
            "understanding_slides": (
                "Return a JSON object with exactly one descriptor per supplied page: "
                '{"descriptors":[{"page":1,"title":null,"topics":[],"concepts":[],"formulas":[],"examples":[],"role":"other"}]}. '
                "Each descriptor must contain exactly these seven fields. page is an integer; title is a string or null; "
                "topics, concepts, formulas, and examples MUST each be an array of strings on every page. "
                "When a page has no formula, formulas MUST be []; never null, a string, or an object. "
                "Use [] for any other absent list. Each of the four arrays must contain at most 20 short strings, "
                "each at most 160 characters; title is at most 160 characters. "
                "role must be one of section_intro, concept_introduction, "
                "concept_explanation, formula, procedure, comparison, example, exercise, summary, transition, other. "
                "Describe each page briefly. Do not create lecture structure, long summaries, future teacher claims, or outside knowledge."
            ),
            "extracting_knowledge": (
                "Return a JSON object with exactly one field, candidates, containing an array. "
                "Each candidate must have exactly these four fields: title (nonempty string), "
                "suggested_type (one of topic, concept, relationship, comparison, procedure, formula), "
                "aliases (array of strings, [] when absent), and source_pages (nonempty array of unique "
                "ascending integer page numbers from the supplied batch only). "
                "Do not add candidate_id; the application assigns it. Discover only independent knowledge "
                "units that can be understood, expanded, and cited. Never create slide/example/note/batch/section "
                "as a unit merely to preserve content. Agenda, outline, recap, course topics summary, references, "
                "thank-you, and non-substantive section dividers are structural slide information, not candidates. "
                "A named protocol or model is a concept even when it describes sender/receiver actions; reserve "
                "procedure for calculation, operational workflow, sender/receiver FSM, or explicit rule-driven logic. "
                "Do not decide hierarchy. Keep the list selective."
            ),
            "building_scaffold": (
                "Return JSON object {nodes:[{key,title,node_type,parent_key,candidate_ids,aliases,scope}],"
                "relations:[{from_key,to_key,type,reason,source_pages}]}. "
                "node_type must be one of topic, concept, relationship, comparison, procedure, formula. "
                "key is a short unique local reference; parent_key is null for roots; candidate_ids and aliases "
                "are arrays of strings. scope is a short PPT-grounded statement of what this node covers, "
                "not a broad summary; do not invent content to fill it. "
                "For every node, candidate_ids must be a nonempty array copied verbatim from the supplied "
                "allowed_candidate_ids. Never use candidate titles, local keys, page numbers, ranges, or newly "
                "invented IDs as candidate_ids. Every supplied candidate ID must appear in at least one node. "
                "Merge duplicate candidates. Order nodes in lecture display order, with parents before children. "
                "A protocol and an acronym/full-name or overview variant of that same protocol are one node; "
                "do not make a separate node for a sender timeout rule, receiver handling branch, or other "
                "detail that belongs in its parent's content. "
                "Classify named protocols (including rdt versions), models, mechanisms, and principles as concept; "
                "sender/receiver FSM or logic and calculations as procedure. The mere presence of actions in a "
                "protocol does not make the protocol node a procedure. Do not make a KnowledgeNode for a slide "
                "that is only agenda, outline, recap, course topics summary, references, or a section divider. "
                "parent-child means conceptual containment, never slide sequence or protocol evolution. "
                "Relations are separate directed semantic claims. type must be depends_on, evolves_to, "
                "contrasts_with, applies_to, example_of, or related_to. Both keys must name nodes; reason must "
                "briefly explain slide-supported meaning; source_pages must cite supporting supplied pages. "
                "Generate only high-value cross-tree relations: old protocol evolves_to newer protocol, explicit "
                "comparison, performance problem to solution, or genuine prerequisite. evolves_to always points "
                "from earlier/less capable to later/improved; never to an abstract service interface. "
                "A parent-child pair needs no semantic relation merely to repeat containment. "
                "Adjacent slides alone never justify parent-child or a relation. "
                "Use relations:[] unless the supplied PPT evidence explicitly supports a meaningful edge."
            ),
            "enriching_nodes": (
                "Return a JSON object with exactly two keys: content and evidence. "
                "Each evidence item has exactly kind, label, text; kind is slide_example and label is null. "
                "Use only the fields allowed for the named node_type. Keep absent text null and absent lists []; "
                "do not fill fields merely because they exist. Concept: definition, assumptions, mechanism, "
                "key_points, key_terms; describe FSM behavior as mechanism, not procedure steps. Procedure: "
                "objective, conditions, numbered steps {order,content}, rules, result. Use steps only for a genuine "
                "sequence; use rules for FSM transitions, event-response behavior, and sender/receiver logic. Formula: expression, variables "
                "{symbol,meaning}, interpretation, assumptions; do not generate steps. Comparison: items, "
                "dimensions {dimension,values}, similarities, differences. Relationship: statement, key_points. "
                "Topic: summary, key_points. Evidence is provenance, not a replacement for typed content. When a "
                "slide states knowledge, place it in the appropriate content field: protocol assumptions and "
                "mechanisms in concept, operational FSM behavior in procedure.rules. Do not return an empty "
                "concept or procedure content object while providing substantive evidence. Evidence may contain "
                "slide examples. Preserve relevant supplied code examples verbatim, including opening/closing "
                "tags, punctuation, case and line breaks, in evidence.text. Split long examples into ordered "
                "evidence entries of at most 700 characters rather than truncating code. Explain supplied "
                "table rows and formula notation without dropping their conditions or symbols. Everything must "
                "be explicitly supported by the supplied PPT slides; no textbook knowledge, inferred assumptions, "
                "or facts generated to fill optional fields."
            ),
            "covering_relations": (
                "Return JSON object with exactly one field relations, an array of missing high-value semantic edges. "
                "Each edge has exactly from_node_id, to_node_id, type, reason, source_pages. Use only supplied node IDs "
                "and relation types depends_on, evolves_to, contrasts_with, applies_to, example_of, related_to. "
                "Find only missing high-value cross-node knowledge relations. Prioritize protocol evolution, explicit "
                "comparison, performance problem to solution, and genuine prerequisites. evolves_to points from "
                "older/less capable to newer/improved, even if the sentence says the newer version evolves from the old. "
                "Never use evolves_to for an abstract service interface. Do not repeat direct parent-child containment. "
                "Suggested pairs are prompts to inspect, not mandatory edges. Return no more than the supplied "
                "remaining_budget. Cite source_pages from the supplied slide text that directly supports the "
                "reason and belongs to either endpoint. Do not infer a relation merely from slide adjacency or numbering. "
                "Use relations:[] when evidence is insufficient."
            ),
        }
        if stage == "enriching_nodes":
            node_type = payload["node"]["node_type"]
            instructions[stage] += (
                f" For this {node_type} node, content must contain exactly these field names: "
                + ", ".join(empty_content(node_type))
                + ". Populate the fields with facts explicitly supported by the supplied slides "
                "within this node's scope. Use null or [] only for fields without supported facts. "
                "When the slides support substantive knowledge for this node, at least one substantive "
                "content field must be populated; do not put all knowledge only in evidence."
                " Content field types are described by this JSON Schema (types, not literal response values): "
                + json.dumps(_content_schema(node_type), ensure_ascii=False)
                + ". In particular, an array field must remain an array even when it contains one fact; "
                "never return a bare string for assumptions, mechanism, key_points, key_terms, or rules."
            )
            if node_type == "procedure":
                instructions[stage] += (
                    " Every rules entry must be one nonempty string. Express each FSM transition as a concise "
                    "string containing only slide-supported state, trigger, action, and next state; never put "
                    "objects or null values inside rules."
                )
            if node_type == "comparison":
                instructions[stage] += (
                    ' For each dimensions entry, return values as an array of {"item":"TCP","value":"slide-supported fact"} '
                    'objects; use [] when no supported values exist. The application converts this array to the canonical item-to-value map. '
                    'Never put a free-form string, null, or an empty object in values.'
                )
        feedback = payload.get("validation_feedback")
        if isinstance(feedback, dict) and isinstance(feedback.get("error"), str):
            instructions[stage] += (
                " Your previous response failed schema validation: "
                + feedback["error"][:300]
                + " Repair using only the supplied slide text/images; do not add outside facts."
                + ". Regenerate the complete JSON response for the same input pages, correcting this field."
            )
        content: str | list[dict[str, Any]] = json.dumps(payload, ensure_ascii=False)
        payload_limit = {"understanding_slides": 18_000, "extracting_knowledge": 22_000, "building_scaffold": 102_000, "enriching_nodes": 14_000, "covering_relations": 70_000}[stage]
        if len(content.encode("utf-8")) > payload_limit or len(self.base_prompt.encode("utf-8")) > 20_000:
            raise ValueError(f"{stage} input exceeds deterministic text budget")
        if stage in {"understanding_slides", "enriching_nodes"}:
            images = [item for item in payload.get("slides", []) if item.get("image_path")]
            if images:
                if self.workspace_root is None:
                    raise ValueError("visual slide workspace is not configured")
                content_parts: list[dict[str, Any]] = [{"type": "text", "text": content}]
                total = 0
                for item in images:
                    path = (self.workspace_root / item["image_path"]).resolve()
                    path.relative_to(self.workspace_root)
                    data = path.read_bytes()
                    total += len(data)
                    if total > 2_000_000 or not data.startswith(b"\x89PNG"):
                        raise ValueError("visual evidence exceeds bounded PNG input")
                    content_parts.append({"type": "text", "text": f"Slide {item['page']} image:"})
                    content_parts.append({"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(data).decode("ascii")}})
                content = content_parts
        if self.client is None:
            from qwen_chat_client import QwenChatClient
            self.client = QwenChatClient()
        response_format = _response_format(stage, payload)
        if not self._json_schema_supported and response_format["type"] == "json_schema":
            response_format = {"type": "json_object"}
        request = dict(
            model=self.model_name,
            temperature=0,
            response_format=response_format,
            extra_body={"enable_thinking": False},
            messages=[{"role": "system", "content": self.base_prompt + "\n\n" + instructions[stage]}, {"role": "user", "content": content}],
        )
        if response_format["type"] == "json_object":
            request["max_tokens"] = 8192 if stage == "building_scaffold" else 4096
        if stage == "building_scaffold":
            configured_timeout = getattr(getattr(self.client, "config", None), "timeout_seconds", 90.0)
            request["timeout"] = max(float(configured_timeout), BUILD_SCAFFOLD_TIMEOUT_SECONDS)
        try:
            response = await self.client.create_chat_completion(**request)
        except Exception as exc:
            # Some model endpoints reject strict schemas even though JSON mode works.
            # Fall back only for a provider-side response_format rejection.
            message = str(exc).lower()
            if response_format["type"] != "json_schema" or getattr(exc, "status_code", None) not in (400, 422) or not any(term in message for term in ("json_schema", "response_format")):
                raise
            self._json_schema_supported = False
            request["response_format"] = {"type": "json_object"}
            request["max_tokens"] = 8192 if stage == "building_scaffold" else 4096
            response = await self.client.create_chat_completion(**request)
        content = response.choices[0].message.content
        if not isinstance(content, str):
            raise ValueError("model returned no JSON text")
        return json.loads(content)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _write(root: Path, relative: str, content: bytes) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _jsonl(rows: Sequence[dict[str, Any]]) -> bytes:
    return b"".join((json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8") for row in rows)


def bounded_batches(items: Sequence[dict[str, Any]], *, max_items: int, max_bytes: int, max_image_bytes: int | None = None, max_image_pixels: int | None = None) -> list[list[dict[str, Any]]]:
    """Reject an oversized single item before any model call."""
    batches: list[list[dict[str, Any]]] = []
    batch: list[dict[str, Any]] = []
    size = 2
    image_size = 0
    image_pixels = 0
    for item in items:
        item_size = len(json.dumps(item, ensure_ascii=False).encode("utf-8")) + 1
        item_image_size = item.get("image_bytes", 0)
        item_image_pixels = item.get("image_pixels", 0)
        if item_size + 2 > max_bytes:
            raise ValueError(f"single slide evidence exceeds {max_bytes} bytes")
        if max_image_bytes is not None and item_image_size > max_image_bytes:
            raise ValueError(f"single slide image exceeds {max_image_bytes} bytes")
        if max_image_pixels is not None and item_image_pixels > max_image_pixels:
            raise ValueError(f"single slide image exceeds {max_image_pixels} pixels")
        if batch and (len(batch) >= max_items or size + item_size > max_bytes or (max_image_bytes is not None and image_size + item_image_size > max_image_bytes) or (max_image_pixels is not None and image_pixels + item_image_pixels > max_image_pixels)):
            batches.append(batch)
            batch, size, image_size, image_pixels = [], 2, 0, 0
        batch.append(item)
        size += item_size
        image_size += item_image_size
        image_pixels += item_image_pixels
    if batch:
        batches.append(batch)
    return batches


def _slide_evidence(slide: dict[str, Any], root: Path) -> dict[str, Any]:
    page = slide["slide_number"]
    lines = []
    for block in slide.get("blocks", []):
        if block.get("include_in_semantic_context") is False:
            continue
        text = block_evidence_text(block)
        if text:
            lines.append(text)
    evidence = {"page": page, "text": supplement_native_pdf_text(slide, root, "\n".join(lines))}
    vision = slide.get("vision")
    if isinstance(vision, dict) and vision.get("required") is True:
        relative = vision.get("slide_image_path")
        if not isinstance(relative, str):
            raise ValueError(f"slide {page} lacks visual page path")
        path = (root / relative).resolve()
        path.relative_to(root)
        with path.open("rb") as handle:
            header = handle.read(24)
        if len(header) < 24 or not header.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError(f"slide {page} has invalid rendered PNG")
        pixels = int.from_bytes(header[16:20], "big") * int.from_bytes(header[20:24], "big")
        if pixels <= 0:
            raise ValueError(f"slide {page} has invalid image dimensions")
        evidence["image_path"] = relative
        evidence["image_bytes"] = path.stat().st_size
        evidence["image_pixels"] = pixels
    return evidence


def _fingerprint(root: Path) -> dict[str, str]:
    files = ["normalized/normalized_slides.json", "source/original.pdf"]
    result = {}
    for relative in files:
        path = root / relative
        if path.is_file():
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    if "normalized/normalized_slides.json" not in result:
        raise ValueError("normalized slides are missing")
    if "source/original.pdf" not in result:
        raise ValueError("source PDF is missing")
    normalized = json.loads((root / "normalized/normalized_slides.json").read_text(encoding="utf-8"))
    for slide in normalized.get("slides", []):
        vision = slide.get("vision")
        if isinstance(vision, dict) and vision.get("required") is True:
            relative = vision["slide_image_path"]
            path = (root / relative).resolve()
            path.relative_to(root)
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _record(root: Path, relative: str) -> dict[str, Any]:
    data = (root / relative).read_bytes()
    return {"path": relative, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


class PreAgentRun:
    def __init__(self, root: Path, lecture_id: str, title: str, model: ScaffoldModel, *, prompt_version: str = PROMPT_VERSION) -> None:
        self.root, self.lecture_id, self.title, self.model = root.resolve(), lecture_id, title, model
        self.prompt_version = prompt_version
        self.input_fingerprint: dict[str, str] | None = None
        self.run_id = f"run_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}_{uuid.uuid4().hex[:12]}"
        self.state: dict[str, Any] = {"schema_version": 1, "run_id": self.run_id, "lecture_id": lecture_id, "status": "running", "current_stage": "parsing_slides", "stages": {name: {"status": "pending", "attempts": 0, "input_scope": None, "error": None} for name in STAGES}, "error": None, "commit": None}

    def save(self) -> None:
        _write(self.root, STATE, canonical_json(self.state))

    def save_invalid_response(self, stage: str, scope: str, attempt: int, error: str, response: Any) -> str | None:
        """Keep a bounded local diagnostic so schema failures can be reproduced."""
        relative = f"runtime/preagent_diagnostics/{self.run_id}/{stage}_{hashlib.sha256(scope.encode()).hexdigest()[:12]}_{attempt}.json"
        try:
            raw = canonical_json(response)
            details = ({"response": response} if len(raw) <= 64_000 else
                {"response_preview": raw[:64_000].decode("utf-8", errors="replace"), "truncated": True})
            _write(self.root, relative, canonical_json({"stage": stage, "scope": scope, "attempt": attempt, "error": error, **details}))
            return relative
        except (OSError, TypeError, ValueError):
            return None

    async def call(self, stage: str, scope: str, payload: dict[str, Any], validate: Callable[[Any], Any]) -> Any:
        step = self.state["stages"][stage]
        step.update(status="running", input_scope=scope, error=None, diagnostic_path=None)
        self.state["current_stage"] = stage
        self.save()
        cache_path: Path | None = None
        if self.input_fingerprint is not None:
            identity = {
                "cache_version": CACHE_VERSION,
                "input_fingerprint": self.input_fingerprint,
                "model": self.model.model_name,
                "base_prompt": getattr(self.model, "base_prompt", ""),
                "prompt_version": self.prompt_version,
                "stage": stage,
                "scope": scope,
                "payload": payload,
            }
            cache_key = hashlib.sha256(canonical_json(identity)).hexdigest()
            cache_path = self.root / "runtime/preagent_cache" / f"{cache_key}.json"
            if cache_path.is_file():
                try:
                    return validate(json.loads(cache_path.read_text(encoding="utf-8")))
                except (OSError, ValueError, TypeError, KeyError):
                    # A stale or corrupt cache entry is never promoted into a new run.
                    pass
        last: Exception | None = None
        request_payload = payload
        for _ in range(MAX_ATTEMPTS):
            step["attempts"] += 1
            self.save()
            generated = False
            try:
                result = await self.model.generate(stage, request_payload)
                generated = True
                validated = validate(result)
                if cache_path is not None:
                    try:
                        _write(self.root, str(cache_path.relative_to(self.root)), canonical_json(result))
                    except (OSError, TypeError, ValueError):
                        # Cache persistence must not invalidate a successful model result.
                        pass
                return validated
            except Exception as exc:  # noqa: BLE001 - preserve actual worker cause
                last = exc
                step["error"] = f"{type(exc).__name__}: {exc}"[:500]
                if generated and isinstance(exc, ScaffoldValidationError):
                    step["diagnostic_path"] = self.save_invalid_response(stage, scope, step["attempts"], str(exc), result)
                self.save()
                if isinstance(exc, ScaffoldValidationError):
                    request_payload = {
                        **payload,
                        "validation_feedback": {"error": str(exc)[:300]},
                    }
        raise PreAgentError(stage, scope, str(last), MAX_ATTEMPTS)

    def finish_stage(self, stage: str) -> None:
        self.state["stages"][stage].update(status="completed", error=None)
        self.save()

    def begin_stage(self, stage: str, scope: str) -> None:
        self.state["current_stage"] = stage
        self.state["stages"][stage].update(status="running", input_scope=scope, error=None)
        if stage in {"parsing_slides", "generating_derived_outputs"} and self.state["stages"][stage]["attempts"] == 0:
            self.state["stages"][stage]["attempts"] = 1
        self.save()

    async def run(self) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / "runtime/preagent.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                return await self._run_locked()
            except Exception as exc:
                self.state["status"] = "failed"
                self.state["error"] = str(exc)[:500]
                current = self.state["current_stage"]
                if current in self.state["stages"]:
                    step = self.state["stages"][current]
                    step["status"] = "failed"
                    step["error"] = str(exc)[:500]
                self.save()
                raise

    async def _run_locked(self) -> dict[str, Any]:
        self.save()
        self.begin_stage("parsing_slides", "normalized/normalized_slides.json")
        input_fingerprint = _fingerprint(self.root)
        self.input_fingerprint = input_fingerprint
        normalized = json.loads((self.root / "normalized/normalized_slides.json").read_text(encoding="utf-8"))
        slides = normalized.get("slides")
        if not isinstance(slides, list) or not slides:
            raise PreAgentError("parsing_slides", "normalized slides", "no slides found")
        evidence = [_slide_evidence(slide, self.root) for slide in slides]
        pages = [item["page"] for item in evidence]
        if pages != list(range(1, len(pages) + 1)):
            raise PreAgentError("parsing_slides", "normalized slides", "pages must be contiguous from 1")
        count = len(pages)
        self.finish_stage("parsing_slides")
        self.begin_stage("understanding_slides", f"pages 1-{count}")
        descriptors: list[dict[str, Any]] = []
        for batch in bounded_batches(evidence, max_items=4, max_bytes=16000, max_image_bytes=2_000_000, max_image_pixels=8_000_000):
            expected = {row["page"] for row in batch}
            def check(value: Any) -> list[dict[str, Any]]:
                if not isinstance(value, dict) or set(value) != {"descriptors"} or not isinstance(value["descriptors"], list):
                    raise ScaffoldValidationError("descriptor response must contain descriptors list")
                rows = [compact_slide_descriptor(row) for row in value["descriptors"]]
                for row in rows:
                    validate_slide_descriptor(row, count)
                if {row["page"] for row in rows} != expected or len(rows) != len(expected):
                    raise ScaffoldValidationError("descriptor pages do not match input scope")
                return sorted(rows, key=lambda row: row["page"])
            descriptors.extend(await self.call("understanding_slides", f"pages {min(expected)}-{max(expected)}", {"slides": batch}, check))
        _write(self.root, ARTIFACTS[0], _jsonl(descriptors))
        self.finish_stage("understanding_slides")

        self.begin_stage("extracting_knowledge", f"pages 1-{count}")
        by_page = {item["page"]: item for item in descriptors}
        extraction_items = [{"page": row["page"], "descriptor": by_page[row["page"]], "slide_text": row["text"]} for row in evidence]
        raw_candidates: list[dict[str, Any]] = []
        for batch in bounded_batches(extraction_items, max_items=8, max_bytes=20000):
            scope_pages = {row["page"] for row in batch}
            def check_candidates(value: Any) -> list[dict[str, Any]]:
                if not isinstance(value, dict) or set(value) != {"candidates"} or not isinstance(value["candidates"], list):
                    raise ScaffoldValidationError("candidate response must contain candidates list")
                result = []
                for index, raw in enumerate(value["candidates"], 1):
                    if not isinstance(raw, dict) or set(raw) != {"title", "suggested_type", "aliases", "source_pages"}:
                        raise ScaffoldValidationError(f"candidate {index}: fields invalid; required: title, suggested_type, aliases, source_pages")
                    raw = dict(raw)
                    if raw["aliases"] is None:
                        raw["aliases"] = []
                    if isinstance(raw["aliases"], list) and all(isinstance(alias, str) for alias in raw["aliases"]):
                        raw["aliases"] = dedupe_casefold_strings([alias.strip() for alias in raw["aliases"] if alias.strip()], limit=10)
                    if isinstance(raw["source_pages"], list) and all(type(page) is int for page in raw["source_pages"]):
                        raw["source_pages"] = sorted(set(raw["source_pages"]))
                    if isinstance(raw["title"], str) and isinstance(raw["suggested_type"], str):
                        raw["suggested_type"] = classify_node_type(raw["title"], "", raw["suggested_type"])
                    item = {"candidate_id": f"KC_{index:04d}", **raw}
                    try:
                        validate_knowledge_candidate(item, count)
                    except ScaffoldValidationError as exc:
                        raise ScaffoldValidationError(f"candidate {index}: {exc}") from exc
                    if not set(item["source_pages"]) <= scope_pages:
                        raise ScaffoldValidationError("candidate cites page outside batch")
                    result.append(item)
                return result
            raw_candidates.extend(await self.call("extracting_knowledge", f"pages {min(scope_pages)}-{max(scope_pages)}", {"slides": batch}, check_candidates))
        candidates = [item for item in dedupe_candidates(raw_candidates) if not is_structural_candidate(item["title"])]
        for index, item in enumerate(candidates, 1):
            item["candidate_id"] = f"KC_{index:04d}"
        if not candidates:
            raise PreAgentError("extracting_knowledge", "whole lecture", "no independent knowledge candidates")
        for item in candidates:
            validate_knowledge_candidate(item, count)
        _write(self.root, ARTIFACTS[1], _jsonl(candidates))
        self.finish_stage("extracting_knowledge")

        self.begin_stage("building_scaffold", "whole lecture descriptors and candidates")
        overview = {
            "descriptors": descriptors,
            "candidates": candidates,
            "allowed_candidate_ids": [item["candidate_id"] for item in candidates],
        }
        if len(canonical_json(overview)) > 100_000:
            raise PreAgentError("building_scaffold", "whole lecture", "global descriptor/candidate payload exceeds 100000 bytes")
        def check_plan(value: Any) -> dict[str, Any]:
            value, dropped_relations = discard_unsupported_builder_relations(value, candidates)
            if not isinstance(value, dict) or set(value) != {"nodes", "relations"} or not isinstance(value["nodes"], list) or not value["nodes"] or not isinstance(value["relations"], list):
                raise ScaffoldValidationError("builder must return nodes and relations")
            specs = value["nodes"]
            keys = set()
            candidate_ids = {item["candidate_id"] for item in candidates}
            used = set()
            for index, spec in enumerate(specs, 1):
                if not isinstance(spec, dict) or set(spec) != {"key", "title", "node_type", "parent_key", "candidate_ids", "aliases", "scope"}:
                    raise ScaffoldValidationError(f"builder node {index}: fields invalid")
                key = spec["key"]
                if not isinstance(key, str) or not key.strip() or key in keys or key.startswith(("sec_", "batch_")):
                    raise ScaffoldValidationError("builder node key invalid")
                keys.add(key)
                if not isinstance(spec["title"], str) or not spec["title"].strip() or spec["node_type"] not in NODE_TYPES:
                    raise ScaffoldValidationError("builder title/type invalid")
                if is_structural_candidate(spec["title"]):
                    raise ScaffoldValidationError("builder node is structural slide information, not a knowledge unit")
                spec["node_type"] = classify_node_type(spec["title"], spec["scope"] if isinstance(spec["scope"], str) else "", spec["node_type"])
                if not isinstance(spec["scope"], str) or not spec["scope"].strip() or len(spec["scope"]) > 700:
                    raise ScaffoldValidationError("builder scope invalid")
                if spec["parent_key"] is not None and spec["parent_key"] not in keys - {key}:
                    raise ScaffoldValidationError("builder parents must precede children")
                refs = spec["candidate_ids"]
                if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) for ref in refs):
                    raise ScaffoldValidationError(f"builder node {key}: candidate_ids must be a nonempty array of exact ID strings")
                unknown = [ref for ref in refs if ref not in candidate_ids]
                if unknown:
                    raise ScaffoldValidationError(
                        f"builder node {key}: unknown candidate_ids {unknown[:5]!r}; "
                        "copy exact IDs from allowed_candidate_ids"
                    )
                used.update(refs)
                if not isinstance(spec["aliases"], list) or any(not isinstance(x, str) or not x.strip() for x in spec["aliases"]):
                    raise ScaffoldValidationError("builder aliases invalid")
            if used != candidate_ids:
                raise ScaffoldValidationError(f"builder omitted candidate_ids {sorted(candidate_ids - used)[:12]!r}; every ID must appear")
            candidate_map = {item["candidate_id"]: item for item in candidates}
            pages_by_key = {spec["key"]: {page for ref in spec["candidate_ids"] for page in candidate_map[ref]["source_pages"]} for spec in specs}
            for relation in value["relations"]:
                if not isinstance(relation, dict) or set(relation) != {"from_key", "to_key", "type", "reason", "source_pages"}:
                    raise ScaffoldValidationError("builder relation fields invalid")
                source, target = relation["from_key"], relation["to_key"]
                if not isinstance(source, str) or not isinstance(target, str) or source not in keys or target not in keys:
                    raise ScaffoldValidationError("builder relation endpoint missing")
                if source == target:
                    raise ScaffoldValidationError("builder relation self reference")
                if relation["type"] not in RELATION_TYPES:
                    raise ScaffoldValidationError("builder relation type invalid")
                if not isinstance(relation["reason"], str) or not relation["reason"].strip() or len(relation["reason"]) > 240:
                    raise ScaffoldValidationError("builder relation reason invalid")
                pages = relation["source_pages"]
                if not isinstance(pages, list) or not pages or any(type(page) is not int for page in pages):
                    raise ScaffoldValidationError("builder relation source_pages invalid")
                relation["source_pages"] = pages = sorted(set(pages))
                if not set(pages) <= pages_by_key[source] | pages_by_key[target]:
                    raise ScaffoldValidationError("builder relation cites page outside endpoint evidence")
            self.state["stages"]["building_scaffold"]["dropped_relations"] = dropped_relations
            return value
        plan = await self.call("building_scaffold", "whole lecture descriptors and candidates", overview, check_plan)
        specs = plan["nodes"]
        candidate_map = {item["candidate_id"]: item for item in candidates}
        ids = {spec["key"]: f"KN_{index:04d}" for index, spec in enumerate(specs, 1)}
        nodes: dict[str, dict[str, Any]] = {}
        for spec in specs:
            node_id = ids[spec["key"]]
            source_pages = sorted({page for candidate_id in spec["candidate_ids"] for page in candidate_map[candidate_id]["source_pages"]})
            parent_key = spec["parent_key"]
            nodes[node_id] = {"node_id": node_id, "title": spec["title"], "node_type": spec["node_type"], "parent_id": ids[parent_key] if parent_key else None, "children": [], "order": 0, "aliases": dedupe_casefold_strings(spec["aliases"]), "scope": spec["scope"], "content": empty_content(spec["node_type"]), "evidence": [], "source_refs": [{"type": "slide", "page": page} for page in source_pages]}
        roots = []
        for node in nodes.values():
            parent = node["parent_id"]
            siblings = nodes[parent]["children"] if parent else roots
            node["order"] = len(siblings)
            siblings.append(node["node_id"])
        relations = normalize_relations([{"from_node_id": ids[relation["from_key"]], "to_node_id": ids[relation["to_key"]], "type": relation["type"], "reason": relation["reason"], "source_refs": [{"type": "slide", "page": page} for page in relation["source_pages"]]} for relation in plan["relations"]])
        scaffold = {"schema_version": "2.0", "lecture_id": self.lecture_id, "title": self.title, "source": {"slide_file": "source/original.pdf", "page_count": count}, "root_nodes": roots, "nodes": nodes, "relations": relations, "metadata": {"preagent_run_id": self.run_id, "created_at": _now(), "model": self.model.model_name, "prompt_version": self.prompt_version}}
        validate_scaffold(scaffold)
        self.finish_stage("building_scaffold")

        self.begin_stage("enriching_nodes", f"{len(nodes)} knowledge nodes")
        evidence_by_page = {row["page"]: row for row in evidence}
        for node in nodes.values():
            if node["node_type"] == "topic":
                continue
            source_pages = [ref["page"] for ref in node["source_refs"]]
            chunks = bounded_batches([evidence_by_page[page] for page in source_pages], max_items=3, max_bytes=12000, max_image_bytes=2_000_000, max_image_pixels=8_000_000)
            pieces = []
            for chunk in chunks:
                scope = f"{node['node_id']} pages {chunk[0]['page']}-{chunk[-1]['page']}"
                pieces.append(await self.call("enriching_nodes", scope, {"node": {"node_id": node["node_id"], "title": node["title"], "node_type": node["node_type"], "scope": node["scope"]}, "slides": chunk}, lambda value, node_type=node["node_type"]: _validated_content_response(value, node_type)))
            node["content"] = merge_typed_content(node["node_type"], [piece["content"] for piece in pieces])
            seen_evidence: set[str] = set()
            for piece in pieces:
                for item in piece["evidence"]:
                    marker = item["text"]
                    if marker not in seen_evidence and len(node["evidence"]) < 100:
                        node["evidence"].append(item)
                        seen_evidence.add(marker)
        validate_scaffold(scaffold)
        self.finish_stage("enriching_nodes")

        scaffold, node_cleanup = consolidate_nodes(scaffold)
        scaffold, initial_relation_cleanup = clean_relations(scaffold)
        nodes = scaffold["nodes"]

        self.begin_stage("covering_relations", "whole lecture evidence and existing relations")
        relation_payload = {
            "nodes": [{"node_id": node["node_id"], "title": node["title"], "node_type": node["node_type"],
                       "parent_id": node["parent_id"], "scope": node["scope"][:160],
                       "source_pages": [ref["page"] for ref in node["source_refs"]]} for node in nodes.values()],
            "existing_relations": [{key: relation[key] for key in ("from_node_id", "to_node_id", "type", "reason")}
                                   for relation in scaffold["relations"]],
            "suggested_pairs_to_check": relation_coverage_candidates(scaffold),
            "remaining_budget": max(0, MAX_RELATIONS_PER_LECTURE - len(scaffold["relations"])),
            "slides": [{"page": row["page"], "text": row["text"][:350]} for row in evidence],
        }
        additions = await self.call("covering_relations", "whole lecture evidence and existing relations", relation_payload,
                                    lambda value: validate_relation_coverage_response(value, nodes, count))
        scaffold["relations"] = normalize_relations([*scaffold["relations"], *additions])
        scaffold, explicit_relation_report = supplement_explicit_relations(scaffold)
        scaffold, final_relation_cleanup = clean_relations(scaffold)
        _write(self.root, f"runtime/preagent_cleanup/{self.run_id}.json",
               canonical_json({"nodes": node_cleanup, "initial_relations": initial_relation_cleanup,
                               "explicit_relations": explicit_relation_report,
                               "final_relations": final_relation_cleanup}))
        validate_scaffold(scaffold)
        self.finish_stage("covering_relations")

        self.begin_stage("generating_derived_outputs", "scaffold and derived artifacts")
        _write(self.root, ARTIFACTS[2], canonical_json(scaffold))
        _write(self.root, ARTIFACTS[3], render_scaffold_markdown(scaffold).encode("utf-8"))
        _write(self.root, ARTIFACTS[4], canonical_json(derive_hotwords(scaffold)))
        _write(self.root, ARTIFACTS[5], derive_asr_context(scaffold).encode("utf-8"))
        self.finish_stage("generating_derived_outputs")
        if _fingerprint(self.root) != input_fingerprint:
            raise PreAgentError("generating_derived_outputs", "input fingerprint", "source changed during run")
        records = [_record(self.root, path) for path in ARTIFACTS]
        manifest_path = MANIFEST.format(run_id=self.run_id)
        manifest = {"schema_version": 1, "run_id": self.run_id, "lecture_id": self.lecture_id, "input_fingerprint": input_fingerprint, "artifacts": records, "created_at": _now()}
        manifest_bytes = canonical_json(manifest)
        _write(self.root, manifest_path, manifest_bytes)
        self.state.update(status="completed", current_stage="completed", commit={"manifest_path": manifest_path, "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(), "committed_at": _now()})
        self.save()
        return {"run_id": self.run_id, "status": "completed", "manifest_path": manifest_path, "artifact_count": len(records)}


def resolve_committed_package(root: Path | str) -> dict[str, Any]:
    root = Path(root).resolve()
    try:
        state = json.loads((root / STATE).read_text(encoding="utf-8"))
        if state["status"] != "completed":
            raise ValueError("run has not completed")
        commit = state["commit"]
        manifest_path = commit["manifest_path"]
        if manifest_path != MANIFEST.format(run_id=state["run_id"]):
            raise ValueError("manifest path mismatch")
        data = (root / manifest_path).read_bytes()
        if hashlib.sha256(data).hexdigest() != commit["manifest_sha256"]:
            raise ValueError("manifest hash mismatch")
        manifest = json.loads(data)
        if manifest["run_id"] != state["run_id"] or manifest["lecture_id"] != state["lecture_id"] or manifest["input_fingerprint"] != _fingerprint(root):
            raise ValueError("manifest identity or input mismatch")
        if [item["path"] for item in manifest["artifacts"]] != list(ARTIFACTS):
            raise ValueError("manifest artifact set mismatch")
        if manifest["artifacts"] != [_record(root, path) for path in ARTIFACTS]:
            raise ValueError("artifact hash mismatch")
        scaffold = json.loads((root / ARTIFACTS[2]).read_text(encoding="utf-8"))
        validate_scaffold(scaffold)
        # v1 JSON was committed with sorted object keys, while its Markdown used the
        # model's original variable order. The manifest already verifies both files.
        if scaffold["schema_version"] != 1 and (root / ARTIFACTS[3]).read_text(encoding="utf-8") != render_scaffold_markdown(scaffold):
            raise ValueError("Markdown differs from scaffold")
        # Hotword selection evolves independently of schema/prompt versions. The
        # manifest hash verifies the originally committed output without making
        # older preparations unreadable after a selection-policy update.
        if scaffold["metadata"]["prompt_version"] == "v2":
            if (root / ARTIFACTS[5]).read_text(encoding="utf-8") != derive_asr_context(scaffold):
                raise ValueError("ASR context differs from scaffold")
    except (OSError, ValueError, KeyError, TypeError, ScaffoldValidationError) as exc:
        raise ValueError(f"invalid committed preparation: {exc}") from exc
    return {"run_id": state["run_id"], "manifest_path": manifest_path, "manifest_sha256": commit["manifest_sha256"]}


def run_preagent(root: Path | str, lecture_id: str, title: str, model: ScaffoldModel | None = None, *, prompt_version: str = PROMPT_VERSION) -> dict[str, Any]:
    return asyncio.run(PreAgentRun(Path(root), lecture_id, title, model or QwenScaffoldModel(workspace_root=Path(root)), prompt_version=prompt_version).run())
