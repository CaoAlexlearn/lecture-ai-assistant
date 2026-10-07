"""Strict Knowledge Scaffold contracts and deterministic derived artifacts."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

ROLES = frozenset({"section_intro", "concept_introduction", "concept_explanation", "formula", "procedure", "comparison", "example", "exercise", "summary", "transition", "other"})
NODE_TYPES = frozenset({"topic", "concept", "relationship", "comparison", "procedure", "formula"})
LEGACY_RELATIONS = ("prerequisites", "related_to", "contrasts_with")
RELATION_TYPES = frozenset({"depends_on", "evolves_to", "contrasts_with", "applies_to", "example_of", "related_to"})
PRELECTURE_FIELDS = ("core_idea", "definition", "key_terms", "formula", "variables", "steps", "comparison", "slide_examples")
NODE_ID = re.compile(r"KN_\d{4,}")
RELATION_ID = re.compile(r"REL-\d{4,}")
CONTENT_FIELDS = {
    "topic": ("summary", "key_points"),
    "concept": ("definition", "assumptions", "mechanism", "key_points", "key_terms"),
    "procedure": ("objective", "conditions", "steps", "rules", "result"),
    "formula": ("expression", "variables", "interpretation", "assumptions"),
    "comparison": ("items", "dimensions", "similarities", "differences"),
    "relationship": ("statement", "key_points"),
}
TEXT_CONTENT_FIELDS = frozenset({"summary", "definition", "objective", "result", "expression", "interpretation", "statement"})
LIST_CONTENT_FIELDS = frozenset({"key_points", "assumptions", "mechanism", "key_terms", "conditions", "items", "similarities", "differences", "rules"})
EVIDENCE_KINDS = frozenset({"slide_example", "slide_note", "legacy_definition", "legacy_formula", "legacy_variable", "legacy_comparison", "legacy_key_term", "legacy_step"})


class ScaffoldValidationError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ScaffoldValidationError(message)


def _keys(value: Any, expected: set[str], label: str) -> None:
    _require(isinstance(value, dict) and set(value) == expected, f"{label}: invalid fields")


def _text(value: Any, label: str, *, nullable: bool = False) -> None:
    _require((nullable and value is None) or (isinstance(value, str) and bool(value.strip())), f"{label}: expected nonempty text{' or null' if nullable else ''}")


def _strings(value: Any, label: str) -> None:
    actual = "null" if value is None else type(value).__name__
    _require(isinstance(value, list), f"{label}: expected list, got {actual}")
    for item in value:
        _text(item, label)
    _require(len({item.casefold() for item in value}) == len(value), f"{label}: duplicates")


def dedupe_casefold_strings(values: Sequence[str], *, limit: int | None = None) -> list[str]:
    """Keep the first spelling while using the validator's duplicate rule."""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            result.append(value)
            if limit is not None and len(result) >= limit:
                break
    return result


def _pages(value: Any, page_count: int, label: str) -> None:
    _require(isinstance(value, list) and all(type(page) is int and 1 <= page <= page_count for page in value), f"{label}: invalid page")
    _require(value == sorted(set(value)), f"{label}: pages must be unique and sorted")


def validate_slide_descriptor(value: Any, page_count: int) -> None:
    _keys(value, {"page", "title", "topics", "concepts", "formulas", "examples", "role"}, "descriptor")
    _require(type(value["page"]) is int and 1 <= value["page"] <= page_count, "descriptor: invalid page")
    _text(value["title"], "descriptor title", nullable=True)
    for field in ("topics", "concepts", "formulas", "examples"):
        _strings(value[field], f"descriptor {field}")
        _require(len(value[field]) <= 20 and all(len(item) <= 160 for item in value[field]), f"descriptor {field}: too much content")
    _require(value["title"] is None or len(value["title"]) <= 160, "descriptor title: too long")
    _require(value["role"] in ROLES, "descriptor: invalid role")


def compact_slide_descriptor(value: Any) -> Any:
    """Bound descriptive cues without changing the underlying slide evidence."""
    if not isinstance(value, dict):
        return value
    result = dict(value)
    result.setdefault("title", None)
    if isinstance(result.get("title"), str):
        result["title"] = result["title"].strip()[:160] or None
    for field in ("topics", "concepts", "formulas", "examples"):
        items = result.get(field)
        if items is None:
            items = []
        elif isinstance(items, str):
            items = [items]
        if isinstance(items, list) and all(isinstance(item, str) for item in items):
            result[field] = dedupe_casefold_strings([item.strip()[:160] for item in items if item.strip()], limit=20)
    return result


def validate_knowledge_candidate(value: Any, page_count: int) -> None:
    _keys(value, {"candidate_id", "title", "suggested_type", "aliases", "source_pages"}, "candidate")
    _require(isinstance(value["candidate_id"], str) and re.fullmatch(r"KC_\d{4,}", value["candidate_id"]) is not None, "candidate: invalid id")
    _text(value["title"], "candidate title")
    _require(len(value["title"]) <= 160, "candidate title: too long")
    suggested_type = value["suggested_type"]
    _require(
        isinstance(suggested_type, str) and suggested_type in NODE_TYPES,
        f"candidate suggested_type: invalid value {str(suggested_type)[:80]!r}; allowed values: {', '.join(sorted(NODE_TYPES))}",
    )
    _strings(value["aliases"], "candidate aliases")
    _require(len(value["aliases"]) <= 10, "candidate aliases: too many")
    _pages(value["source_pages"], page_count, "candidate source_pages")
    _require(bool(value["source_pages"]), "candidate: missing provenance")


def empty_prelecture() -> dict[str, Any]:
    return {"core_idea": None, "definition": None, "key_terms": [], "formula": None, "variables": {}, "steps": [], "comparison": None, "slide_examples": []}


def validate_prelecture(value: Any) -> None:
    _keys(value, set(PRELECTURE_FIELDS), "prelecture")
    for key in ("core_idea", "definition", "formula"):
        _text(value[key], f"prelecture {key}", nullable=True)
        _require(value[key] is None or len(value[key]) <= 700, f"prelecture {key}: too long")
    for key in ("key_terms", "steps", "slide_examples"):
        _strings(value[key], f"prelecture {key}")
        _require(len(value[key]) <= 20 and all(len(item) <= 350 for item in value[key]), f"prelecture {key}: too much content")
    variable_type = "null" if value["variables"] is None else type(value["variables"]).__name__
    _require(isinstance(value["variables"], dict), f"prelecture variables: expected object, got {variable_type}")
    _require(len(value["variables"]) <= 20, "prelecture variables: too many")
    for key, meaning in value["variables"].items():
        _text(key, "variable name")
        _text(meaning, "variable meaning")
        _require(len(key) <= 40 and len(meaning) <= 350, "prelecture variable: too long")
    _require(value["comparison"] is None or isinstance(value["comparison"], dict), "prelecture comparison: expected object or null")
    if isinstance(value["comparison"], dict):
        _require(bool(value["comparison"]), "prelecture comparison: empty object")
        _require(len(value["comparison"]) <= 20, "prelecture comparison: too many items")
        for key, text in value["comparison"].items():
            _text(key, "comparison key")
            _text(text, "comparison value")
            _require(len(text) <= 350, "comparison value: too long")


def validate_knowledge_node_v1(value: Any, node_id: str, page_count: int) -> None:
    _keys(value, {"node_id", "title", "node_type", "parent_id", "children", "order", "aliases", "prelecture", "source_refs", "relations"}, f"node {node_id}")
    _require(value["node_id"] == node_id and NODE_ID.fullmatch(node_id) is not None, f"node {node_id}: id mismatch")
    _text(value["title"], f"node {node_id} title")
    _require(len(value["title"]) <= 160, f"node {node_id}: title too long")
    _require(value["node_type"] in NODE_TYPES, f"node {node_id}: invalid type")
    _require(value["parent_id"] is None or isinstance(value["parent_id"], str), f"node {node_id}: invalid parent")
    _strings(value["children"], f"node {node_id} children")
    _require(type(value["order"]) is int and value["order"] >= 0, f"node {node_id}: invalid order")
    _strings(value["aliases"], f"node {node_id} aliases")
    validate_prelecture(value["prelecture"])
    refs = value["source_refs"]
    _require(isinstance(refs, list) and bool(refs), f"node {node_id}: missing provenance")
    pages = []
    for ref in refs:
        _keys(ref, {"type", "page"}, f"node {node_id} source ref")
        _require(ref["type"] == "slide", f"node {node_id}: invalid source type")
        pages.append(ref["page"])
    _pages(pages, page_count, f"node {node_id} source refs")
    _keys(value["relations"], set(LEGACY_RELATIONS), f"node {node_id} relations")
    for relation in LEGACY_RELATIONS:
        _strings(value["relations"][relation], f"node {node_id} {relation}")


def _validate_scaffold_v1(value: Any) -> None:
    _keys(value, {"schema_version", "lecture_id", "title", "source", "root_nodes", "nodes", "metadata"}, "scaffold")
    _require(value["schema_version"] == 1, "scaffold: invalid version")
    _text(value["lecture_id"], "lecture_id")
    _text(value["title"], "title")
    _keys(value["source"], {"slide_file", "page_count"}, "source")
    _text(value["source"]["slide_file"], "slide_file")
    count = value["source"]["page_count"]
    _require(type(count) is int and count > 0, "source: invalid page_count")
    _keys(value["metadata"], {"preagent_run_id", "created_at", "model", "prompt_version"}, "metadata")
    for key in value["metadata"]:
        _text(value["metadata"][key], f"metadata {key}")
    nodes = value["nodes"]
    _require(isinstance(nodes, dict) and bool(nodes), "scaffold: missing nodes")
    _strings(value["root_nodes"], "root_nodes")
    for node_id, node in nodes.items():
        validate_knowledge_node_v1(node, node_id, count)
    ids = set(nodes)
    roots = value["root_nodes"]
    _require(bool(roots) and set(roots) == {key for key, node in nodes.items() if node["parent_id"] is None}, "scaffold: root_nodes mismatch")
    seen_children: set[str] = set()
    for node_id, node in nodes.items():
        parent = node["parent_id"]
        _require(parent is None or parent in ids, f"node {node_id}: orphan parent")
        _require(len(set(node["children"])) == len(node["children"]), f"node {node_id}: duplicate child")
        for child in node["children"]:
            _require(child in ids and nodes[child]["parent_id"] == node_id, f"node {node_id}: inconsistent child {child}")
            seen_children.add(child)
        for relation in LEGACY_RELATIONS:
            _require(all(target in ids and target != node_id for target in node["relations"][relation]), f"node {node_id}: invalid relation")
    _require(seen_children == ids - set(roots), "scaffold: orphan or missing child")
    _require(all(nodes[root]["parent_id"] is None for root in roots), "scaffold: invalid root")
    for siblings in [roots, *(node["children"] for node in nodes.values())]:
        _require([nodes[node_id]["order"] for node_id in siblings] == list(range(len(siblings))), "scaffold: sibling order mismatch")
    def visit(node_id: str, stack: set[str], visited: set[str]) -> None:
        _require(node_id not in stack, "scaffold: cycle")
        if node_id in visited:
            return
        stack.add(node_id)
        for child in nodes[node_id]["children"]:
            visit(child, stack, visited)
        stack.remove(node_id)
        visited.add(node_id)
    visited: set[str] = set()
    for root in roots:
        visit(root, set(), visited)
    _require(visited == ids, "scaffold: unreachable node")


def empty_content(node_type: str) -> dict[str, Any]:
    if node_type not in CONTENT_FIELDS:
        raise ScaffoldValidationError(f"content: invalid node type {node_type!r}")
    result: dict[str, Any] = {}
    for field in CONTENT_FIELDS[node_type]:
        result[field] = None if field in TEXT_CONTENT_FIELDS else []
    return result


def compact_typed_content(node_type: str, value: Any) -> dict[str, Any]:
    """Normalize equivalent worker representations before strict canonical validation."""
    _require(node_type in CONTENT_FIELDS, "content: invalid node type")
    _require(isinstance(value, dict), f"{node_type} content: expected object")
    unexpected = set(value) - set(CONTENT_FIELDS[node_type])
    _require(not unexpected, f"{node_type} content: invalid fields {sorted(unexpected)!r}")
    result = empty_content(node_type)
    item_overview: dict[str, str] = {}
    for field, item in value.items():
        if field in TEXT_CONTENT_FIELDS and isinstance(item, str):
            text = item.strip()
            result[field] = (text if field == "expression" else text[:700]) or None
        elif field == "items" and isinstance(item, list) and any(isinstance(entry, dict) for entry in item):
            names: list[str] = []
            valid = True
            for entry in item:
                if isinstance(entry, str):
                    name, description = entry, None
                elif isinstance(entry, dict) and set(entry) == {"item", "value"}:
                    name, description = entry["item"], entry["value"]
                elif isinstance(entry, dict) and set(entry) == {"name", "description"}:
                    name, description = entry["name"], entry["description"]
                else:
                    valid = False
                    break
                if not isinstance(name, str) or not name.strip() or description is not None and (not isinstance(description, str) or not description.strip()):
                    valid = False
                    break
                name = name.strip()[:120]
                names.append(name)
                if isinstance(description, str):
                    description = description.strip()[:350]
                    _require(name not in item_overview or item_overview[name] == description,
                             "comparison content items: conflicting descriptions")
                    item_overview[name] = description
            _require(valid, "comparison content items: expected names or {item,value} pairs")
            result[field] = dedupe_casefold_strings(names, limit=20)
        elif node_type == "procedure" and field == "rules" and isinstance(item, list):
            rules: list[str] = []
            for entry in item:
                if isinstance(entry, str):
                    rules.append(entry)
                elif isinstance(entry, dict) and set(entry) <= {"state", "trigger", "action", "next_state"}:
                    parts = []
                    for key, label in (("state", "State"), ("trigger", "trigger"), ("action", "action"), ("next_state", "next state")):
                        detail = entry.get(key)
                        if isinstance(detail, str) and detail.strip():
                            parts.append(f"{label}: {detail.strip()[:75]}")
                    if parts:
                        rules.append("; ".join(parts))
                else:
                    rules.append(entry)
            if all(isinstance(rule, str) for rule in rules):
                result[field] = dedupe_casefold_strings([rule.strip()[:350] for rule in rules if rule.strip()], limit=20)
            else:
                result[field] = rules
        elif field in LIST_CONTENT_FIELDS:
            if item is None:
                result[field] = []
            elif isinstance(item, list) and all(isinstance(text, str) for text in item):
                result[field] = dedupe_casefold_strings([text.strip()[:350] for text in item if text.strip()], limit=20)
            else:
                result[field] = item
        elif field == "steps" and isinstance(item, list):
            steps: list[dict[str, Any]] = []
            seen: set[str] = set()
            for step in item:
                text = step if isinstance(step, str) else step.get("content") if isinstance(step, dict) and set(step) <= {"order", "content"} else None
                if not isinstance(text, str):
                    steps.append(step)
                    continue
                text = text.strip()[:350]
                if text and text.casefold() not in seen and len(steps) < 20:
                    steps.append({"order": len(steps) + 1, "content": text})
                    seen.add(text.casefold())
            result[field] = steps
        elif field == "variables" and isinstance(item, dict):
            result[field] = [{"symbol": symbol, "meaning": meaning} for symbol, meaning in item.items()]
        elif field == "variables" and isinstance(item, list):
            variables: list[dict[str, Any]] = []
            seen_symbols: dict[str, str] = {}
            for variable in item:
                if not isinstance(variable, dict) or set(variable) != {"symbol", "meaning"}:
                    variables.append(variable)
                    continue
                symbol, meaning = variable["symbol"], variable["meaning"]
                if not isinstance(symbol, str) or not isinstance(meaning, str):
                    variables.append(variable)
                    continue
                symbol, meaning = symbol.strip()[:40], meaning.strip()[:350]
                if not symbol or not meaning:
                    continue
                key = symbol
                if key in seen_symbols and seen_symbols[key] != meaning:
                    variables.append({"symbol": symbol, "meaning": meaning})
                elif key not in seen_symbols and len(variables) < 20:
                    variables.append({"symbol": symbol, "meaning": meaning})
                    seen_symbols[key] = meaning
            result[field] = variables
        elif field == "dimensions" and isinstance(item, list):
            dimensions: list[Any] = []
            by_name: dict[str, dict[str, Any]] = {}
            for dimension in item:
                if not isinstance(dimension, dict) or set(dimension) != {"dimension", "values"}:
                    dimensions.append(dimension)
                    continue
                name = dimension["dimension"]
                if not isinstance(name, str) or not name.strip():
                    dimensions.append(dimension)
                    continue
                name = name.strip()[:120]
                values = dimension["values"]
                if values is None or values == [] or values == {}:
                    continue
                if isinstance(values, list):
                    mapped: dict[str, str] = {}
                    valid = True
                    for entry in values:
                        if isinstance(entry, dict) and set(entry) == {"item", "value"}:
                            item_name, text = entry["item"], entry["value"]
                        elif isinstance(entry, dict) and len(entry) == 1:
                            item_name, text = next(iter(entry.items()))
                        else:
                            valid = False
                            break
                        if isinstance(text, list) and all(isinstance(part, str) for part in text):
                            text = "; ".join(part.strip() for part in text if part.strip())
                        if not isinstance(item_name, str) or not isinstance(text, str) or not item_name.strip() or not text.strip():
                            valid = False
                            break
                        item_name, text = item_name.strip()[:120], text.strip()[:350]
                        if item_name in mapped and mapped[item_name] != text:
                            valid = False
                            break
                        mapped[item_name] = text
                    _require(valid, "comparison content dimensions: values must be nonempty {item,value} pairs")
                    values = mapped
                elif isinstance(values, dict):
                    mapped = {}
                    for item_name, text in values.items():
                        if isinstance(text, list) and all(isinstance(part, str) for part in text):
                            text = "; ".join(part.strip() for part in text if part.strip())
                        if not isinstance(item_name, str) or not isinstance(text, str) or not item_name.strip() or not text.strip():
                            continue
                        item_name, text = item_name.strip()[:120], text.strip()[:350]
                        _require(item_name not in mapped or mapped[item_name] == text,
                                 "comparison content dimensions: conflicting values for the same item")
                        if len(mapped) < 20:
                            mapped[item_name] = text
                    values = mapped
                if isinstance(values, dict) and not values:
                    continue
                key = name.casefold()
                if key in by_name and isinstance(values, dict) and isinstance(by_name[key]["values"], dict):
                    existing = by_name[key]["values"]
                    for item_name, text in values.items():
                        _require(item_name not in existing or existing[item_name] == text,
                                 "comparison content dimensions: conflicting values for the same item")
                        if len(existing) < 20:
                            existing[item_name] = text
                elif len(dimensions) < 20:
                    entry = {"dimension": name, "values": values}
                    dimensions.append(entry)
                    by_name[key] = entry
            result[field] = dimensions
        elif field in {"steps", "variables", "dimensions"} and item is None:
            result[field] = []
        else:
            result[field] = item
    if item_overview:
        dimensions = result["dimensions"]
        _require(isinstance(dimensions, list), "comparison content dimensions: expected list")
        overview = next((entry for entry in dimensions if isinstance(entry, dict) and entry.get("dimension") == "Overview"), None)
        if overview is None:
            _require(len(dimensions) < 20, "comparison content dimensions: no room for item descriptions")
            dimensions.append({"dimension": "Overview", "values": item_overview})
        else:
            existing = overview.get("values")
            _require(isinstance(existing, dict), "comparison content dimensions: invalid Overview values")
            for name, description in item_overview.items():
                _require(name not in existing or existing[name] == description,
                         "comparison content dimensions: conflicting Overview values")
                _require(name in existing or len(existing) < 20,
                         "comparison content dimensions: too many Overview values")
                existing[name] = description
    validate_content(node_type, result)
    return result


def validate_content(node_type: str, value: Any) -> None:
    _require(node_type in CONTENT_FIELDS, "content: invalid node type")
    # Existing committed v2 packages predate procedure.rules. Keep them readable;
    # new workers always emit the five-field procedure shape.
    expected = set(CONTENT_FIELDS[node_type])
    actual = set(value) if isinstance(value, dict) else set()
    _require(isinstance(value, dict) and (actual == expected or node_type == "procedure" and actual == expected - {"rules"}),
             f"{node_type} content: invalid fields")
    for field, item in value.items():
        label = f"{node_type} content {field}"
        if field in TEXT_CONTENT_FIELDS:
            _text(item, label, nullable=True)
            _require(item is None or len(item) <= 700, f"{label}: too long")
        elif field in LIST_CONTENT_FIELDS:
            _strings(item, label)
            _require(len(item) <= 20 and all(len(text) <= 350 for text in item), f"{label}: too much content")
        elif field == "steps":
            _require(isinstance(item, list) and len(item) <= 20, f"{label}: expected bounded list")
            for index, step in enumerate(item, 1):
                _keys(step, {"order", "content"}, f"{label} step")
                _require(type(step["order"]) is int and step["order"] == index, f"{label}: steps out of order")
                _text(step["content"], f"{label} step")
                _require(len(step["content"]) <= 350, f"{label}: step too long")
        elif field == "variables":
            _require(isinstance(item, list) and len(item) <= 20, f"{label}: expected bounded list")
            symbols: set[str] = set()
            for variable in item:
                _keys(variable, {"symbol", "meaning"}, f"{label} variable")
                _text(variable["symbol"], f"{label} symbol")
                _text(variable["meaning"], f"{label} meaning")
                _require(len(variable["symbol"]) <= 40 and len(variable["meaning"]) <= 350, f"{label}: variable too long")
                _require(variable["symbol"] not in symbols, f"{label}: duplicate symbol")
                symbols.add(variable["symbol"])
        elif field == "dimensions":
            _require(isinstance(item, list) and len(item) <= 20, f"{label}: expected bounded list")
            dimensions: set[str] = set()
            for dimension in item:
                _keys(dimension, {"dimension", "values"}, f"{label} dimension")
                _text(dimension["dimension"], f"{label} name")
                _require(len(dimension["dimension"]) <= 120, f"{label}: name too long")
                _require(dimension["dimension"].casefold() not in dimensions, f"{label}: duplicate dimension")
                dimensions.add(dimension["dimension"].casefold())
                values = dimension["values"]
                actual = "null" if values is None else type(values).__name__
                _require(isinstance(values, dict) and bool(values) and len(values) <= 20, f"{label}: invalid values; expected nonempty item-to-text map, got {actual}")
                for key, text in values.items():
                    _text(key, f"{label} item")
                    _text(text, f"{label} value")
                    _require(len(key) <= 120 and len(text) <= 350, f"{label}: value too long")


def validate_content_population(node_type: str, content: dict[str, Any], evidence: list[dict[str, Any]]) -> None:
    """Reject a worker's evidence-only shell, without imposing textbook-filled fields."""
    validate_content(node_type, content)
    validate_evidence(evidence)
    if not evidence:
        return
    substantive = {
        "topic": ("summary", "key_points"),
        "concept": ("definition", "assumptions", "mechanism", "key_points"),
        "procedure": ("objective", "conditions", "steps", "rules", "result"),
        "formula": ("expression", "variables", "interpretation", "assumptions"),
        "comparison": ("items", "dimensions", "similarities", "differences"),
        "relationship": ("statement", "key_points"),
    }[node_type]
    _require(any(content.get(field) for field in substantive),
             "CONTENT_UNDERPOPULATED: slide evidence exists but typed content is empty; populate only facts explicitly supported by these slides")


def merge_typed_content(node_type: str, pieces: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Merge bounded worker batches without inferring missing facts."""
    merged = empty_content(node_type)
    for piece in pieces:
        validate_content(node_type, piece)
        for field, value in piece.items():
            if field in TEXT_CONTENT_FIELDS:
                if merged[field] is None and value is not None:
                    merged[field] = value
            elif field in LIST_CONTENT_FIELDS:
                merged[field] = dedupe_casefold_strings(merged[field] + value, limit=20)
            elif field == "steps":
                seen = {step["content"].casefold() for step in merged[field]}
                for step in value:
                    if step["content"].casefold() not in seen and len(merged[field]) < 20:
                        merged[field].append({"order": len(merged[field]) + 1, "content": step["content"]})
                        seen.add(step["content"].casefold())
            elif field == "variables":
                seen = {variable["symbol"] for variable in merged[field]}
                for variable in value:
                    if variable["symbol"] not in seen and len(merged[field]) < 20:
                        merged[field].append(variable)
                        seen.add(variable["symbol"])
            elif field == "dimensions":
                by_name = {item["dimension"].casefold(): item for item in merged[field]}
                for dimension in value:
                    key = dimension["dimension"].casefold()
                    if key not in by_name and len(merged[field]) < 20:
                        copy = {"dimension": dimension["dimension"], "values": dict(dimension["values"])}
                        merged[field].append(copy)
                        by_name[key] = copy
                    elif key in by_name:
                        for item, text in dimension["values"].items():
                            if item not in by_name[key]["values"] and len(by_name[key]["values"]) < 20:
                                by_name[key]["values"][item] = text
    validate_content(node_type, merged)
    return merged


def validate_evidence(value: Any) -> None:
    _require(isinstance(value, list) and len(value) <= 100, "evidence: expected bounded list")
    for item in value:
        _keys(item, {"kind", "label", "text"}, "evidence item")
        _require(item["kind"] in EVIDENCE_KINDS, "evidence: invalid kind")
        _text(item["label"], "evidence label", nullable=True)
        _text(item["text"], "evidence text")
        _require((item["label"] is None or len(item["label"]) <= 120) and len(item["text"]) <= 700, "evidence: too long")


def compact_worker_evidence(value: Any) -> list[dict[str, Any]]:
    """Keep optional slide evidence even when a JSON-mode worker varies its label."""
    if value is None:
        return []
    if isinstance(value, (str, dict)):
        value = [value]
    _require(isinstance(value, list), "enrichment evidence: expected list")
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if isinstance(item, str):
            kind, label, content = "slide_note", None, item
        elif isinstance(item, dict):
            content = item.get("text", item.get("content"))
            original_kind = item.get("kind")
            kind = "slide_example" if original_kind == "slide_example" else "slide_note"
            label = item.get("label")
            if not isinstance(label, str) or not label.strip():
                label = original_kind if isinstance(original_kind, str) and original_kind.strip() and kind == "slide_note" else None
        else:
            continue
        if not isinstance(content, str):
            continue
        content = content.strip()
        if not content:
            continue
        label = label.strip()[:120] if isinstance(label, str) and label.strip() else None
        marker = (kind, content)
        if marker not in seen:
            # Preserve long source examples instead of silently cutting off a
            # closing tag, formula condition, or final code line at character 700.
            chunks = [content[start:start + 700] for start in range(0, len(content), 700)]
            _require(len(result) + len(chunks) <= 100, "evidence exceeds bounded entry budget")
            for chunk in chunks:
                if chunk.strip():
                    result.append({"kind": kind, "label": label, "text": chunk})
            seen.add(marker)
    validate_evidence(result)
    return result


def validate_relation(value: Any, nodes: Mapping[str, Any], page_count: int) -> None:
    _keys(value, {"relation_id", "from_node_id", "to_node_id", "type", "reason", "source_refs"}, "relation")
    _require(isinstance(value["relation_id"], str) and RELATION_ID.fullmatch(value["relation_id"]) is not None, "relation: invalid id")
    source, target = value["from_node_id"], value["to_node_id"]
    _require(isinstance(source, str) and source in nodes, "relation: missing source")
    _require(isinstance(target, str) and target in nodes, "relation: missing target")
    _require(source != target, "relation: self reference")
    _require(value["type"] in RELATION_TYPES, "relation: invalid type")
    _text(value["reason"], "relation reason")
    _require(len(value["reason"]) <= 240, "relation: reason too long")
    refs = value["source_refs"]
    _require(isinstance(refs, list), "relation: invalid source_refs")
    pages: list[int] = []
    for ref in refs:
        _keys(ref, {"type", "page"}, "relation source ref")
        _require(ref["type"] == "slide", "relation: invalid source type")
        pages.append(ref["page"])
    _pages(pages, page_count, "relation source refs")
    endpoint_pages = {ref["page"] for node_id in (source, target) for ref in nodes[node_id]["source_refs"]}
    _require(set(pages) <= endpoint_pages, "relation: source page outside endpoint evidence")


def normalize_relations(relations: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate identical directed claims without inventing or merging their rationale."""
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for relation in relations:
        key = (relation["from_node_id"], relation["to_node_id"], relation["type"])
        if key in seen:
            continue
        seen.add(key)
        result.append({"relation_id": f"REL-{len(result) + 1:04d}", **{k: v for k, v in relation.items() if k != "relation_id"}})
    return result


def _validate_scaffold_v2(value: Any) -> None:
    _keys(value, {"schema_version", "lecture_id", "title", "source", "root_nodes", "nodes", "relations", "metadata"}, "scaffold")
    _require(value["schema_version"] == "2.0", "scaffold: invalid version")
    _text(value["lecture_id"], "lecture_id")
    _text(value["title"], "title")
    _keys(value["source"], {"slide_file", "page_count"}, "source")
    _text(value["source"]["slide_file"], "slide_file")
    count = value["source"]["page_count"]
    _require(type(count) is int and count > 0, "source: invalid page_count")
    _keys(value["metadata"], {"preagent_run_id", "created_at", "model", "prompt_version"}, "metadata")
    for key in value["metadata"]:
        _text(value["metadata"][key], f"metadata {key}")
    nodes = value["nodes"]
    _require(isinstance(nodes, dict) and bool(nodes), "scaffold: missing nodes")
    _strings(value["root_nodes"], "root_nodes")
    ids = set(nodes)
    roots = value["root_nodes"]
    _require(bool(roots) and set(roots) == {key for key, node in nodes.items() if isinstance(node, dict) and node.get("parent_id") is None}, "scaffold: root_nodes mismatch")
    seen_children: set[str] = set()
    for node_id, node in nodes.items():
        _keys(node, {"node_id", "title", "node_type", "parent_id", "children", "order", "aliases", "scope", "content", "evidence", "source_refs"}, f"node {node_id}")
        _require(node["node_id"] == node_id and NODE_ID.fullmatch(node_id) is not None, f"node {node_id}: id mismatch")
        _text(node["title"], f"node {node_id} title")
        _require(len(node["title"]) <= 160, f"node {node_id}: title too long")
        _require(node["node_type"] in NODE_TYPES, f"node {node_id}: invalid type")
        _text(node["scope"], f"node {node_id} scope")
        _require(len(node["scope"]) <= 700, f"node {node_id}: scope too long")
        _require(node["parent_id"] is None or node["parent_id"] in ids, f"node {node_id}: orphan parent")
        _strings(node["children"], f"node {node_id} children")
        _require(type(node["order"]) is int and node["order"] >= 0, f"node {node_id}: invalid order")
        _strings(node["aliases"], f"node {node_id} aliases")
        validate_content(node["node_type"], node["content"])
        validate_evidence(node["evidence"])
        refs = node["source_refs"]
        _require(isinstance(refs, list) and bool(refs), f"node {node_id}: missing provenance")
        pages: list[int] = []
        for ref in refs:
            _keys(ref, {"type", "page"}, f"node {node_id} source ref")
            _require(ref["type"] == "slide", f"node {node_id}: invalid source type")
            pages.append(ref["page"])
        _pages(pages, count, f"node {node_id} source refs")
        for child in node["children"]:
            _require(child in ids and nodes[child]["parent_id"] == node_id, f"node {node_id}: inconsistent child {child}")
            seen_children.add(child)
    _require(seen_children == ids - set(roots), "scaffold: orphan or missing child")
    for siblings in [roots, *(node["children"] for node in nodes.values())]:
        _require([nodes[node_id]["order"] for node_id in siblings] == list(range(len(siblings))), "scaffold: sibling order mismatch")
    _require(isinstance(value["relations"], list), "scaffold: relations must be a list")
    relation_ids: set[str] = set()
    relation_keys: set[tuple[str, str, str]] = set()
    for relation in value["relations"]:
        validate_relation(relation, nodes, count)
        relation_id = relation["relation_id"]
        key = (relation["from_node_id"], relation["to_node_id"], relation["type"])
        _require(relation_id not in relation_ids, "relation: duplicate id")
        _require(key not in relation_keys, "relation: duplicate edge")
        relation_ids.add(relation_id)
        relation_keys.add(key)
    def visit(node_id: str, stack: set[str], visited: set[str]) -> None:
        _require(node_id not in stack, "scaffold: cycle")
        if node_id in visited:
            return
        stack.add(node_id)
        for child in nodes[node_id]["children"]:
            visit(child, stack, visited)
        stack.remove(node_id)
        visited.add(node_id)
    visited: set[str] = set()
    for root in roots:
        visit(root, set(), visited)
    _require(visited == ids, "scaffold: unreachable node")


def validate_scaffold(value: Any) -> None:
    _require(isinstance(value, dict), "scaffold: expected object")
    if value.get("schema_version") == 1:
        _validate_scaffold_v1(value)
    elif value.get("schema_version") == "2.0":
        _validate_scaffold_v2(value)
    else:
        raise ScaffoldValidationError("scaffold: invalid version")


def _migrate_prelecture(node_type: str, pre: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    content = empty_content(node_type)
    evidence: list[dict[str, Any]] = []
    def add(kind: str, text: str, label: str | None = None) -> None:
        evidence.append({"kind": kind, "label": label, "text": text})
    mapped: set[str] = set()
    if node_type == "topic":
        if pre["definition"]:
            content["summary"] = pre["definition"]
            mapped.add("definition")
        content["key_points"] = list(pre["steps"])
        mapped.add("steps")
    elif node_type == "concept":
        content["definition"] = pre["definition"]
        content["mechanism"] = list(pre["steps"])
        content["key_terms"] = list(pre["key_terms"])
        terms = {term.casefold() for term in pre["key_terms"]}
        if "no bit errors" in terms:
            content["assumptions"].append("No bit errors")
        if "no packet loss" in terms or "no loss of packets" in terms:
            content["assumptions"].append("No packet loss")
        mapped.update({"definition", "steps", "key_terms"})
    elif node_type == "procedure":
        content["objective"] = pre["definition"]
        content["steps"] = [{"order": index, "content": text} for index, text in enumerate(pre["steps"], 1)]
        mapped.update({"definition", "steps"})
    elif node_type == "formula":
        content["expression"] = pre["formula"]
        content["variables"] = [{"symbol": key, "meaning": text} for key, text in pre["variables"].items()]
        content["interpretation"] = pre["definition"]
        mapped.update({"formula", "variables", "definition"})
    elif node_type == "comparison":
        if pre["comparison"]:
            content["items"] = list(pre["comparison"])
            content["dimensions"] = [{"dimension": "Overview", "values": dict(pre["comparison"])}]
        mapped.add("comparison")
    elif node_type == "relationship":
        content["statement"] = pre["definition"]
        content["key_points"] = list(pre["steps"])
        mapped.update({"definition", "steps"})
    for field, kind in (("definition", "legacy_definition"), ("formula", "legacy_formula")):
        if field not in mapped and pre[field]:
            add(kind, pre[field])
    for field, kind in (("steps", "legacy_step"), ("key_terms", "legacy_key_term"), ("slide_examples", "slide_example")):
        if field not in mapped:
            for text in pre[field]:
                add(kind, text)
    for field, kind in (("variables", "legacy_variable"), ("comparison", "legacy_comparison")):
        if field not in mapped:
            for label, text in (pre[field] or {}).items():
                add(kind, text, label)
    return content, evidence


def normalize_scaffold(scaffold: dict[str, Any]) -> dict[str, Any]:
    """Read old canonical packages as v2 without modifying their manifest or files."""
    validate_scaffold(scaffold)
    if scaffold["schema_version"] == "2.0":
        return scaffold
    nodes: dict[str, dict[str, Any]] = {}
    relations: list[dict[str, Any]] = []
    legacy_kinds = {"prerequisites": "depends_on", "related_to": "related_to", "contrasts_with": "contrasts_with"}
    for node_id, node in scaffold["nodes"].items():
        content, evidence = _migrate_prelecture(node["node_type"], node["prelecture"])
        nodes[node_id] = {key: node[key] for key in ("node_id", "title", "node_type", "parent_id", "children", "order", "aliases", "source_refs")}
        nodes[node_id].update(scope=node["prelecture"]["core_idea"] or node["title"], content=content, evidence=evidence)
        for old_kind, new_kind in legacy_kinds.items():
            for target in node["relations"][old_kind]:
                relations.append({"from_node_id": node_id, "to_node_id": target, "type": new_kind,
                    "reason": f"Migrated from v1 {old_kind}; rationale unavailable.", "source_refs": []})
    result = {**{key: scaffold[key] for key in ("lecture_id", "title", "source", "root_nodes", "metadata")},
        "schema_version": "2.0", "nodes": nodes, "relations": normalize_relations(relations)}
    validate_scaffold(result)
    return result


def dedupe_candidates(candidates: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge exact normalized names without creating knowledge from batch identities."""
    merged: dict[str, dict[str, Any]] = {}
    for item in candidates:
        key = " ".join(item["title"].casefold().split())
        if key not in merged:
            merged[key] = {**item, "aliases": list(item["aliases"]), "source_pages": list(item["source_pages"])}
        else:
            prior = merged[key]
            prior["source_pages"] = sorted(set(prior["source_pages"] + item["source_pages"]))
            prior["aliases"] = dedupe_casefold_strings(prior["aliases"] + item["aliases"], limit=10)
    result = list(merged.values())
    for index, item in enumerate(result, 1):
        item["candidate_id"] = f"KC_{index:04d}"
    return result


def render_scaffold_markdown(scaffold: dict[str, Any]) -> str:
    validate_scaffold(scaffold)
    if scaffold["schema_version"] == "2.0":
        lines = [f"# {scaffold['title']}", "", f"Source: {scaffold['source']['slide_file']}", ""]
        nodes = scaffold["nodes"]
        def render_v2(node_id: str, depth: int) -> None:
            node = nodes[node_id]
            pages = ", ".join(str(ref["page"]) for ref in node["source_refs"])
            indent = "  " * depth
            lines.append(f"{indent}- **{node['title']}** ({node['node_type']}, {node_id}; slides {pages})")
            lines.append(f"{indent}  - Scope: {node['scope']}")
            for field, value in sorted(node["content"].items()):
                if value:
                    lines.append(f"{indent}  - {field.replace('_', ' ').title()}: {json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (list, dict)) else value}")
            for item in node["evidence"]:
                label = item["label"] or item["kind"].replace("_", " ").title()
                lines.append(f"{indent}  - Evidence ({label}): {item['text']}")
            for child in node["children"]:
                render_v2(child, depth + 1)
        for root in scaffold["root_nodes"]:
            render_v2(root, 0)
        if scaffold["relations"]:
            lines.extend(["", "## Relations", ""])
            for relation in scaffold["relations"]:
                pages = ", ".join(str(ref["page"]) for ref in relation["source_refs"])
                citation = f" (slides {pages})" if pages else ""
                lines.append(f"- {nodes[relation['from_node_id']]['title']} — {relation['type']} → {nodes[relation['to_node_id']]['title']}: {relation['reason']}{citation}")
        return "\n".join(lines) + "\n"
    lines = [f"# {scaffold['title']}", "", f"Source: {scaffold['source']['slide_file']}", ""]
    nodes = scaffold["nodes"]
    def render(node_id: str, depth: int) -> None:
        node = nodes[node_id]
        pages = ", ".join(str(ref["page"]) for ref in node["source_refs"])
        lines.append(f"{'  ' * depth}- **{node['title']}** ({node['node_type']}, {node_id}; slides {pages})")
        pre = node["prelecture"]
        for field in ("core_idea", "definition", "formula"):
            if pre[field]:
                lines.append(f"{'  ' * (depth + 1)}- {field.replace('_', ' ').title()}: {pre[field]}")
        for field in ("key_terms", "steps", "slide_examples"):
            if pre[field]:
                lines.append(f"{'  ' * (depth + 1)}- {field.replace('_', ' ').title()}: {'; '.join(pre[field])}")
        if pre["variables"]:
            lines.append(f"{'  ' * (depth + 1)}- Variables: {'; '.join(f'{key}: {val}' for key, val in pre['variables'].items())}")
        if pre["comparison"]:
            lines.append(f"{'  ' * (depth + 1)}- Comparison: {'; '.join(f'{key}: {val}' for key, val in pre['comparison'].items())}")
        for child in node["children"]:
            render(child, depth + 1)
    for root in scaffold["root_nodes"]:
        render(root, 0)
    return "\n".join(lines) + "\n"


def derive_hotwords(scaffold: dict[str, Any], *, max_terms: int = 30) -> dict[str, Any]:
    validate_scaffold(scaffold)
    if scaffold["schema_version"] == 1:
        return _derive_hotwords_legacy(scaffold, max_terms=max_terms)
    terms: dict[str, dict[str, Any]] = {}
    alias_groups: dict[str, str] = {}
    for node in scaffold["nodes"].values():
        for label in (node["title"], *node["aliases"]):
            match = re.search(r"^(.{3,60}?)\s*\(([A-Z][A-Z0-9]{1,7})\)", label)
            if match:
                group = hotword_key(match.group(2))
                alias_groups[hotword_key(match.group(1))] = group
                alias_groups[group] = group

    def add(raw: str, pages: list[int], origin: int, *, from_title: bool = False) -> None:
        raw = " ".join(raw.split()).strip(" :;,.–-")
        if not raw or len(raw) > 80:
            return
        if re.search(r"U_?\{?sender\}?", raw, re.I):
            raw = "U_sender"
        variants = split_hotword(raw, from_title=from_title)
        for term in variants:
            term = term.strip()
            call = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\([^()]*\)", term)
            if call:
                term = f"{call.group(1)}()"
            if not term or not is_safe_asr_hotword(term):
                continue
            if from_title and re.fullmatch(r"[A-Za-z]+", term) and not is_high_value_hotword(term):
                continue
            key = hotword_key(term)
            if not key or key in {"acknowledgements", "acknowledgments"}:
                continue
            priority = "high" if is_high_value_hotword(term) else "medium"
            # Node titles and explicit multiword glossary terms are already
            # source-grounded. A fixed subject-word allowlist drops important
            # terms when the lecture changes topic.
            if priority == "medium" and origin == 1:
                continue
            if priority == "medium" and not from_title and len(re.findall(r"[A-Za-z]+", term)) < 2:
                continue
            score = (20 if re.fullmatch(r"(?i:rdt)\d+(?:\.\d+)+", term) else 0) + \
                    (18 if term.casefold() in {"go-back-n", "selective repeat"} else 0) + \
                    (12 if re.fullmatch(r"[A-Z][A-Z0-9]{1,7}", term) else 0) + \
                    (25 if from_title and "(" in raw and term != raw else 0) + \
                    (-20 if len(re.findall(r"[A-Za-z]+", term)) > 5 else 0) + origin * 10
            if key not in terms:
                terms[key] = {"term": term, "priority": priority, "source_slides": set(pages),
                              "score": score, "group": alias_groups.get(key, key)}
            else:
                current = terms[key]
                current["source_slides"].update(pages)
                current["score"] = max(current["score"], score)
                if priority == "high":
                    current["priority"] = "high"
                if "_" in term and "_" not in current["term"]:
                    current["term"] = term

    for node in scaffold["nodes"].values():
        pages = [ref["page"] for ref in node["source_refs"]]
        if node["node_type"] not in {"topic", "comparison"}:
            add(node["title"], pages, 3, from_title=True)
            for alias in node["aliases"]:
                add(alias, pages, 2, from_title=True)
        for term in node["content"].get("key_terms", []):
            # Explicit glossary terms outrank broad section/node titles in every
            # subject. Keep the existing speakability and short-acronym filters.
            add(term, pages, 4)
        # Formula symbols remain in the scaffold, not in an audio vocabulary.
        # Evidence prose supports the scaffold but is not an explicit glossary.
        # Harvesting every capitalized fragment here admitted generic tokens
        # such as US and HK as high-priority ASR hints.
    ranked = sorted(terms.values(), key=lambda item: (item["priority"] != "high", -item["score"],
                                                    -len(item["source_slides"]), item["term"].casefold()))
    selected: list[dict[str, Any]] = []
    high_count = 0
    group_count: dict[str, int] = {}
    for item in ranked:
        if len(selected) >= min(max_terms, 50):
            break
        if group_count.get(item["group"], 0) >= 2:
            continue
        priority = "medium" if item["priority"] == "high" and high_count >= 25 else item["priority"]
        selected.append({"term": item["term"], "priority": priority,
                         "source_slides": sorted(item["source_slides"])})
        group_count[item["group"]] = group_count.get(item["group"], 0) + 1
        high_count += priority == "high"
    return {"schema_version": "1.0", "lecture_id": scaffold["lecture_id"], "hotwords": selected}


def _derive_hotwords_legacy(scaffold: dict[str, Any], *, max_terms: int) -> dict[str, Any]:
    terms: dict[str, dict[str, Any]] = {}
    for node in scaffold["nodes"].values():
        pages = [ref["page"] for ref in node["source_refs"]]
        entries = [(node["title"], "high"), *[(alias, "medium") for alias in node["aliases"]], *[(term, "medium") for term in node["prelecture"]["key_terms"]]]
        for term, priority in entries:
            term = " ".join(term.split())
            if not term or len(term) > 80:
                continue
            key = term.casefold()
            if key not in terms:
                terms[key] = {"term": term, "priority": priority, "source_slides": pages}
            else:
                current = terms[key]
                current["source_slides"] = sorted(set(current["source_slides"] + pages))
                if priority == "high":
                    current["priority"] = "high"
    return {"schema_version": "1.0", "lecture_id": scaffold["lecture_id"], "hotwords": list(terms.values())[:max_terms]}


def hotword_key(term: str) -> str:
    call = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\([^()]*\)", term.strip())
    if call:
        term = call.group(1)
    normalized = re.sub(r"\bsize\s+n\b", "size", term.casefold())
    normalized = normalized.replace("acknowledgements", "acknowledgment").replace("acknowledgments", "acknowledgment")
    normalized = normalized.replace("acknowledgement", "acknowledgment")
    return re.sub(r"[^a-z0-9]+", "", normalized)


def is_low_value_hotword(term: str) -> bool:
    if len(term) == 1:
        return True
    # Short location abbreviations are common in slide prose and can make a
    # streaming recognizer repeat them during unrelated speech.
    if term.casefold() in {"us", "hk", "uk", "eu"}:
        return True
    if re.search(r"\b(?:overview|summary|agenda|outline|topics|context|principles|sender fsm|receiver fsm|sender logic|receiver logic)\b", term, re.I):
        return True
    return term.casefold() in {"buffer", "buffering", "timeout", "retransmission", "duplicate data", "in-flight packet", "in-flight packets",
                               "no", "yes", "add", "temp", "ok", "sender", "receiver", "action", "event", "extract", "corrupt", "notcorrupt",
                               "get", "api", "http", "https", "dns", "snmp", "segment", "segments", "unordered", "end systems", "application layer",
                               "transport protocol", "network-layer address", "reliable data transfer protocol", "error detection", "packet loss",
                               "duplicate packet", "out-of-order packet", "window size n", "sequence number (seq #)", "sender utilization"}


def normalize_asr_hotword(term: str) -> str:
    """Prefer the speakable expansion when a glossary term includes an acronym."""

    value = " ".join(term.split())
    leading = re.fullmatch(r"([A-Z][A-Z0-9]{1,7})\s*\(([^()]*)\)", value)
    if leading and len(re.findall(r"[A-Za-z]{2,}", leading.group(2))) >= 2:
        value = leading.group(2)
    else:
        value = re.sub(r"\s*\([A-Z][A-Z0-9]{1,7}\)\s*", " ", value)
    return " ".join(value.split())


def is_safe_asr_hotword(term: str) -> bool:
    """Only send distinctive, speakable terms to the streaming recognizer.

    Short acronyms and formula notation remain in the lecture scaffold, where
    they can inform notes, but they are too easy to hallucinate as audio hints.
    """

    value = normalize_asr_hotword(term)
    if not value or is_low_value_hotword(value):
        return False
    if re.fullmatch(r"[A-Z]{2,3}", value):
        return False
    if re.search(r"[\\$_{}=/()]", value):
        return False
    return True


def hotword_bias_weight(term: str, priority: str) -> int:
    """Use conservative bias for terms admitted to the ASR vocabulary."""

    return 2 if priority.strip().lower() == "high" else 1


def is_high_value_hotword(term: str) -> bool:
    return bool(re.fullmatch(r"[A-Z][A-Z0-9]{1,7}", term) or
                re.fullmatch(r"(?i:rdt)\d+(?:\.\d+)+", term) or
                re.fullmatch(r"(?i:nextseqnum|sendbase|rcvbase|[a-z]/[a-z])", term) or
                "_" in term or re.search(r"[a-z][A-Z]", term) or
                term.casefold() in {"go-back-n", "selective repeat", "stop-and-wait"})


def split_hotword(term: str, *, from_title: bool) -> list[str]:
    versions = re.findall(r"\b(?i:rdt)\d+(?:\.\d+)+\b", term)
    if versions:
        return versions
    variable = re.search(r"\(([A-Za-z][A-Za-z0-9_]*)\)$", term)
    if variable and ("_" in variable.group(1) or re.fullmatch(r"[A-Z]{2,8}", term[:variable.start()].strip())):
        return [variable.group(1)]
    leading_acronym = re.match(r"^([A-Z]{2,8})\s+\([^)]{3,}\)$", term)
    if leading_acronym:
        expansion = term[leading_acronym.end(1):].strip(" ()")
        return [leading_acronym.group(1), expansion]
    acronym = re.search(r"\(([A-Z][A-Z0-9]{1,7})\)", term)
    if acronym:
        base = term[:acronym.start()].strip()
        return [base, acronym.group(1)] if base and not is_low_value_hotword(base) else [acronym.group(1)]
    if from_title:
        acronyms = re.findall(r"\b[A-Z][A-Z0-9]{1,7}\b", term)
        if " vs " in term.casefold() and acronyms:
            return acronyms
        if acronyms:
            return [term, *acronyms]
        stop_wait = re.search(r"\bStop-and-Wait\b", term, re.I)
        if stop_wait:
            return ["Stop-and-Wait"]
        return [term]
    return [term]


def derive_asr_context(scaffold: dict[str, Any], *, max_chars: int = 300) -> str:
    validate_scaffold(scaffold)
    if max_chars <= 0:
        return ""
    prefix = f"Lecture: {scaffold['title'][:120]}. Topics: "
    if max_chars <= len(prefix) + 1:
        return prefix[:max(0, max_chars - 1)].rstrip() + "\n"
    count = scaffold["source"]["page_count"]
    buckets: list[list[tuple[int, int, str]]] = [[], [], []]
    versions: list[tuple[tuple[int, ...], int]] = []
    seen: set[str] = set()
    for node in scaffold["nodes"].values():
        title = node["title"]
        page = min(ref["page"] for ref in node["source_refs"])
        version = re.search(r"\b(?i:rdt)(\d+(?:\.\d+)+)\b", title)
        if version:
            versions.append((tuple(map(int, version.group(1).split("."))), page))
            continue
        if is_low_value_hotword(title) or node["node_type"] == "procedure":
            continue
        label = concise_asr_label(title)
        key = label.casefold()
        if not label or key in seen:
            continue
        seen.add(key)
        score = (3 if node["children"] else 1) + (2 if re.search(r"\b[A-Z]{2,}\b|\bvs\b", title) else 0)
        bucket = min(2, (page - 1) * 3 // count)
        buckets[bucket].append((-score, page, label))
    if versions:
        ordered = sorted(versions)
        first = ".".join(map(str, ordered[0][0]))
        last = ".".join(map(str, ordered[-1][0]))
        label = f"reliable data transfer rdt{first}–rdt{last}" if first != last else f"reliable data transfer rdt{first}"
        buckets[1].append((-5, min(page for _, page in versions), label))
    for bucket in buckets:
        bucket.sort()
    selected: list[str] = []
    while any(buckets):
        for bucket in buckets:
            if not bucket:
                continue
            _, _, label = bucket.pop(0)
            candidate = prefix + "; ".join([*selected, label])
            if len(candidate) + 1 <= max_chars:
                selected.append(label)
    return (prefix + "; ".join(selected)).rstrip() + "\n"


def concise_asr_label(title: str) -> str:
    title = " ".join(title.split())
    acronym = re.fullmatch(r"([A-Z]{2,8})\s+\([^)]+\)", title)
    if acronym:
        return acronym.group(1)
    if re.search(r"\bvs\b", title, re.I):
        return re.sub(r"\s+vs\s+", "/", re.sub(r"\s+Service Models?$", " service models", title, flags=re.I), flags=re.I)
    if title.startswith("Performance of "):
        return title.removeprefix("Performance of ") + " performance"
    if title.casefold() == "pipelined protocols":
        return "pipelining"
    return title


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
