"""Source-bound output shapes shared by every enrichment course and provider."""
from __future__ import annotations

CONTRACT_VERSION = "source_bound_output_v1"


def _object(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def _array(items):
    return {"type": "array", "items": items}


def _string(values=()):
    result = {"type": "string"}
    values = list(dict.fromkeys(values))
    if values:
        result["enum"] = values
    return result


def output_schema(stage, payload):
    """Bind IDs to this request. Semantic evidence still needs program validation."""
    string = _string()
    nullable = {"type": ["string", "null"]}
    if stage == "mapping":
        materials = payload.get("materials", {})
        node_id = _string(n["node_id"] for n in materials.get("nodes", []))
        unit_id = _string(u["unit_id"] for u in payload.get("target_units", []))
        relation = _object({"node_id": node_id, "reason": string,
                            "contribution": string, "angle": string})
        gap = _object({"trigger": _string(["important_uncovered", "corrected_term", "material_mismatch"]),
                       "query": string})
        fragment = _object({
            "description": string,
            "ranges": _array(_object({"unit_id": unit_id, "start": {"type": "integer"},
                                      "end": {"type": "integer"}})),
            "types": _array(_string(["explanation", "reasoning", "procedure", "example",
                                      "qualification", "emphasis", "correction", "assessment"])),
            "status": _string(["mapped", "unmatched", "needs_review", "nonknowledge"]),
            "reason": string, "uncertainty": nullable, "assessment": {"type": "boolean"},
            "links": _array(relation), "gap": {"anyOf": [{"type": "null"}, gap]},
        })
        return _object({"fragments": _array(fragment)})
    if stage == "enrichment":
        fragment_id = _string(f["fragment_id"] for f in payload.get("fragments", []))
        shared_id = _string(s["shared_content_id"] for s in payload.get("shared_content", []))
        pages = sorted({p["page"] for p in payload.get("pages", [])})
        page = {"type": "integer", **({"enum": pages} if pages else {})}
        evidence = _object({"page": page, "quote": string, "source": _string(["slide", "scaffold"])})
        item = _object({
            "heading": string, "text": string, "fragment_ids": _array(fragment_id),
            "source_nature": _string(["explaining_course_material", "lecturer_addition",
                                       "emphasis_or_correction", "uncertain"]),
            "course_evidence": _array(evidence), "addition_basis": nullable,
            "shared_content_ids": _array(shared_id), "uncertainty": nullable,
        })
        return _object({
            "node_id": _string([payload["node"]["node_id"]]), "items": _array(item),
            "omitted_fragments": _array(_object({"fragment_id": fragment_id, "reason": string})),
        })
    # Retrieval's existing ID and page validation remains authoritative.
    return None


def output_contract(stage, payload):
    schema = output_schema(stage, payload)
    if schema is None:
        return None
    rules = ["Return only the response described by schema; do not return this contract.",
             "Identifiers and evidence pages must come from this request."]
    if stage == "mapping":
        rules.extend(["nonknowledge is only a status. For filler use status=nonknowledge, types=[], links=[].",
                      "mapped needs a supported node link; all other statuses require links=[].",
                      "Use exact character offsets within supplied text and allowed_ranges; never estimate offsets."])
    else:
        rules.extend(["Copy node.node_id exactly; example identifiers are not actual output values.",
                      "Every input fragment must contribute to an item or have an explicit omission reason.",
                      "Quotes must be literal supplied evidence; uncertain is preferable to an invented citation."])
    return {"version": CONTRACT_VERSION, "schema": schema, "rules": rules}
