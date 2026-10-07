"""Conservative, source-preserving cleanup of an enriched v2 scaffold."""

from __future__ import annotations

import re
from collections import Counter
from copy import deepcopy
from typing import Any

from knowledge_scaffold import (
    dedupe_casefold_strings,
    merge_typed_content,
    normalize_relations,
    validate_relation,
    validate_scaffold,
)

MAX_RELATIONS_PER_LECTURE = 15
MAX_RELATIONS_PER_NODE = 4
_VERSION = re.compile(r"\b([a-z][a-z0-9-]*?)\s*(\d+(?:\.\d+)+)\b", re.I)
_DETAIL = re.compile(r"\b(?:limitation|problem|vulnerab|trade.off|performance|sender|receiver|fsm|logic|mechanism|operation|handling|buffered|sequence number|checksum|format|comparison)\b", re.I)
_FOLD_DETAIL = re.compile(r"\b(?:timeout and retransmission mechanism|out.of.order packet handling|buffered packet delivery)\b", re.I)


def _version(title: str) -> tuple[str, tuple[int, ...]] | None:
    match = _VERSION.search(title)
    return (match.group(1).casefold(), tuple(map(int, match.group(2).split(".")))) if match else None


def _identity(title: str) -> str:
    title = re.sub(r"\([^)]{2,8}\)", "", title.casefold())
    title = re.sub(r"\b(?:protocol|overview)\b", "", title)
    return re.sub(r"[^a-z0-9]+", "", title)


def _same_knowledge(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if a["node_type"] != b["node_type"]:
        return False
    direct_family = a["parent_id"] == b["parent_id"] or b["parent_id"] == a["node_id"]
    if not direct_family:
        return False
    if _identity(a["title"]) == _identity(b["title"]):
        return True
    av, bv = _version(a["title"]), _version(b["title"])
    simple_version = re.fullmatch(r"[a-z][a-z0-9-]*\s*\d+(?:\.\d+)+(?:\s+protocol)?(?:\s+overview)?", b["title"], re.I)
    if av is not None and av == bv and simple_version:
        return True
    return False


def _too_fine(parent: dict[str, Any], child: dict[str, Any]) -> bool:
    if (child["parent_id"] != parent["node_id"] or child["children"] or len(child["source_refs"]) != 1
            or parent["node_type"] != "procedure" or child["node_type"] not in {"concept", "procedure"}):
        return False
    if not _FOLD_DETAIL.search(child["title"]):
        return False
    direction = re.search(r"\b(sender|receiver)\b", parent["title"], re.I)
    return bool(direction and (direction.group(1).casefold() in child["title"].casefold()
                               or direction.group(1).casefold() == "receiver" and "buffered packet delivery" in child["title"].casefold()))


def _merge_nodes(scaffold: dict[str, Any], keep_id: str, remove_id: str, *, fold: bool) -> None:
    nodes = scaffold["nodes"]
    keep, remove = nodes[keep_id], nodes[remove_id]
    if not fold and keep["node_type"] == remove["node_type"]:
        keep["content"] = merge_typed_content(keep["node_type"], [keep["content"], remove["content"]])
    elif keep["node_type"] == "procedure":
        detail = remove["content"]
        rules = [*keep["content"]["rules"], *detail.get("rules", []),
                 *detail.get("mechanism", []), *detail.get("key_points", [])]
        keep["content"]["rules"] = dedupe_casefold_strings(rules, limit=20)
    keep["aliases"] = dedupe_casefold_strings([*keep["aliases"], remove["title"], *remove["aliases"]], limit=20)
    keep["source_refs"] = [{"type": "slide", "page": page} for page in sorted({ref["page"] for node in (keep, remove) for ref in node["source_refs"]})]
    seen = {(item["kind"], item["text"].casefold()) for item in keep["evidence"]}
    for item in remove["evidence"]:
        marker = (item["kind"], item["text"].casefold())
        if marker not in seen and len(keep["evidence"]) < 100:
            keep["evidence"].append(item)
            seen.add(marker)
    for child_id in remove["children"]:
        nodes[child_id]["parent_id"] = keep_id
        keep["children"].append(child_id)
    if remove["parent_id"] == keep_id:
        keep["children"] = [child_id for child_id in keep["children"] if child_id != remove_id]
    else:
        siblings = nodes[remove["parent_id"]]["children"] if remove["parent_id"] else scaffold["root_nodes"]
        siblings.remove(remove_id)
    for relation in scaffold["relations"]:
        for side in ("from_node_id", "to_node_id"):
            if relation[side] == remove_id:
                relation[side] = keep_id
    del nodes[remove_id]


def consolidate_nodes(scaffold: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Merge clear duplicate protocol objects and fold narrow FSM details."""
    validate_scaffold(scaffold)
    result = deepcopy(scaffold)
    report: dict[str, Any] = {"nodes_before": len(result["nodes"]), "merged": [], "folded": []}
    for remove_id in list(result["nodes"]):
        if remove_id not in result["nodes"]:
            continue
        remove = result["nodes"][remove_id]
        parent_id = remove["parent_id"]
        candidates = ([parent_id] if parent_id else []) + [node_id for node_id, node in result["nodes"].items()
                                                           if node_id != remove_id and node["parent_id"] == parent_id]
        keep_id = next((node_id for node_id in candidates if node_id in result["nodes"] and
                        int(node_id.split("_")[-1]) < int(remove_id.split("_")[-1]) and
                        _same_knowledge(result["nodes"][node_id], remove)), None)
        fold = False
        if keep_id is None and parent_id in result["nodes"] and _too_fine(result["nodes"][parent_id], remove):
            keep_id, fold = parent_id, True
        if keep_id is None:
            continue
        report["folded" if fold else "merged"].append({"from": remove_id, "into": keep_id,
                                                         "title": remove["title"]})
        _merge_nodes(result, keep_id, remove_id, fold=fold)
    for siblings in [result["root_nodes"], *(node["children"] for node in result["nodes"].values())]:
        for order, node_id in enumerate(siblings):
            result["nodes"][node_id]["order"] = order
    result["relations"] = normalize_relations([relation for relation in result["relations"]
                                               if relation["from_node_id"] != relation["to_node_id"]])
    report["nodes_after"] = len(result["nodes"])
    validate_scaffold(result)
    return result, report


def _relation_priority(relation: dict[str, Any]) -> int:
    return {"evolves_to": 0, "contrasts_with": 1, "depends_on": 2,
            "applies_to": 3, "related_to": 4, "example_of": 5}[relation["type"]]


def supplement_explicit_relations(scaffold: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    """Project clear version chains and two-sided slide comparisons into cross-node edges."""
    result = deepcopy(scaffold)
    nodes = result["nodes"]
    existing = {(r["from_node_id"], r["type"], r["to_node_id"]) for r in result["relations"]}
    additions: list[dict[str, Any]] = []

    def add(source: str, target: str, kind: str, reason: str, pages: set[int]) -> None:
        if not pages or (source, kind, target) in existing:
            return
        relation = {"relation_id": "REL-0001", "from_node_id": source, "to_node_id": target,
                    "type": kind, "reason": reason[:240].rstrip(),
                    "source_refs": [{"type": "slide", "page": page} for page in sorted(pages)]}
        validate_relation(relation, nodes, result["source"]["page_count"])
        additions.append(relation)
        existing.add((source, kind, target))

    groups: dict[tuple[str | None, str], list[tuple[tuple[int, ...], str]]] = {}
    for node_id, node in nodes.items():
        version = _version(node["title"])
        if (version and node["node_type"] == "concept" and
                not re.search(r"\b(?:problem|limitation|fatal flaw|vulnerability|performance)\b", node["title"], re.I)):
            groups.setdefault((node["parent_id"], version[0]), []).append((version[1], node_id))
    for group in groups.values():
        by_version: dict[tuple[int, ...], str] = {}
        for version, node_id in sorted(group):
            by_version.setdefault(version, node_id)
        ordered = [by_version[version] for version in sorted(by_version)]
        for source, target in zip(ordered, ordered[1:]):
            if (source, "evolves_to", target) in existing:
                continue
            detail = nodes[target]["scope"].strip().rstrip(".")
            reason = f"{nodes[target]['title']} follows {nodes[source]['title']} in the versioned protocol sequence: {detail}."
            pages = {max(ref["page"] for ref in nodes[source]["source_refs"]),
                     min(ref["page"] for ref in nodes[target]["source_refs"])}
            add(source, target, "evolves_to", reason, pages)

    names: dict[str, str] = {}
    for node_id, node in nodes.items():
        if node["node_type"] == "concept":
            for label in (node["title"], *node["aliases"]):
                names.setdefault(_identity(label), node_id)
    for node in nodes.values():
        if node["node_type"] != "comparison":
            continue
        comparison_pages = {ref["page"] for ref in node["source_refs"]}
        for dimension in node["content"].get("dimensions", []):
            labels = list(dimension.get("values", {}))
            if len(labels) != 2:
                continue
            source, target = (names.get(_identity(label)) for label in labels)
            if not source or not target or source == target:
                continue
            if (source, "contrasts_with", target) in existing or (target, "contrasts_with", source) in existing:
                continue
            endpoint_pages = {ref["page"] for endpoint in (source, target) for ref in nodes[endpoint]["source_refs"]}
            pages = comparison_pages & endpoint_pages
            if pages:
                reason = f"The lecture explicitly compares {labels[0]} and {labels[1]}: {dimension['dimension']}."
                add(source, target, "contrasts_with", reason, {min(pages)})
    result["relations"] = normalize_relations([*result["relations"], *additions])
    return result, {"added_explicit_relations": len(additions)}


def clean_relations(scaffold: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    """Keep only supported cross-tree claims; enforce old-to-new evolution."""
    result = deepcopy(scaffold)
    nodes = result["nodes"]
    stats = {"candidate_relations": len(result["relations"]), "accepted_relations": 0,
             "repaired_direction": 0, "rejected_direction": 0,
             "rejected_tree_redundancy": 0, "rejected_duplicate": 0,
             "rejected_unsupported": 0, "rejected_budget": 0}
    accepted: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    degree: Counter[str] = Counter()
    for relation in sorted(result["relations"], key=_relation_priority):
        relation = dict(relation)
        source, target = relation["from_node_id"], relation["to_node_id"]
        if source not in nodes or target not in nodes or source == target or not relation.get("source_refs"):
            stats["rejected_unsupported"] += 1
            continue
        if relation["type"] == "contrasts_with" and (nodes[source]["node_type"] == "comparison" or
                                                       nodes[target]["node_type"] == "comparison"):
            stats["rejected_unsupported"] += 1
            continue
        if nodes[source]["parent_id"] == target or nodes[target]["parent_id"] == source:
            stats["rejected_tree_redundancy"] += 1
            continue
        if relation["type"] == "evolves_to":
            sv, tv = _version(nodes[source]["title"]), _version(nodes[target]["title"])
            if sv and tv and sv[0] == tv[0] and sv[1] != tv[1]:
                if sv[1] > tv[1]:
                    source, target = target, source
                    relation["from_node_id"], relation["to_node_id"] = source, target
                    stats["repaired_direction"] += 1
            elif re.search(r"\b(?:problem|limitation|vulnerab|drawback)\b", nodes[target]["title"], re.I) and sv:
                source, target = target, source
                relation["from_node_id"], relation["to_node_id"] = source, target
                stats["repaired_direction"] += 1
            elif not (re.search(r"\b(?:problem|limitation|vulnerab|drawback)\b", nodes[source]["title"], re.I)
                      and tv and re.search(r"address|solve|handle|motivat|overcom", relation["reason"], re.I)):
                stats["rejected_direction"] += 1
                continue
            if nodes[source]["parent_id"] == target or nodes[target]["parent_id"] == source:
                stats["rejected_tree_redundancy"] += 1
                continue
        key = (source, target, relation["type"])
        if key in seen:
            stats["rejected_duplicate"] += 1
            continue
        try:
            validate_relation({**relation, "relation_id": "REL-0001"}, nodes, result["source"]["page_count"])
        except (ValueError, TypeError, KeyError):
            stats["rejected_unsupported"] += 1
            continue
        if len(accepted) >= MAX_RELATIONS_PER_LECTURE or degree[source] >= MAX_RELATIONS_PER_NODE or degree[target] >= MAX_RELATIONS_PER_NODE:
            stats["rejected_budget"] += 1
            continue
        accepted.append(relation)
        seen.add(key)
        degree[source] += 1
        degree[target] += 1
    result["relations"] = normalize_relations(accepted)
    stats["accepted_relations"] = len(result["relations"])
    validate_scaffold(result)
    return result, stats
