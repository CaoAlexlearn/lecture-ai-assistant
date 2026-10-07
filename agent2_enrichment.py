"""Resumable, source-bound post-ASR mapping and node enrichment. No ASR writes."""
from __future__ import annotations

import asyncio
import copy
import fcntl
import json
import re
from pathlib import Path

from agent1_v1 import (ModelSpec, _fingerprint, _json_bytes, _parse_json, _response_text,
                       _retryable_provider_error, _retry_delay, _write_json, _write_jsonl)
from agent2_enrichment_adapter import ASRAdapter, require
from agent2_retrieval import Agent2Error, EVIDENCE_PROVENANCE, read_jsonl
from agent2_output_contracts import CONTRACT_VERSION, output_contract, output_schema

PROMPTS = Path(__file__).parent / "agent2_enrichment_prompts"
TYPES = {"explanation", "reasoning", "procedure", "example", "qualification", "emphasis", "correction", "assessment"}
STATUSES = {"mapped", "unmatched", "needs_review", "nonknowledge"}
NATURES = {"explaining_course_material", "lecturer_addition", "emphasis_or_correction", "uncertain"}


def text(value):
    require(isinstance(value, str) and bool(value.strip()), "Expected nonempty source-language text")
    return value


def source_language_only(value, source):
    """Guard against automatic Chinese glosses for sources with no Chinese speech.

    Language preservation beyond this script check is a semantic prompt/evaluation
    requirement; this is deliberately not advertised as a language classifier.
    """
    def has_han(s):
        return any("\u3400" <= c <= "\u9fff" for c in s)
    require(not has_han(value) or has_han(source), "Generated Chinese gloss has no source-language speech")


def material_strings(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for item in value for s in material_strings(item)]
    if isinstance(value, dict):
        return [s for item in value.values() for s in material_strings(item)]
    return []


class RunLease:
    """An OS lock, also preventing duplicate launches across server processes."""
    def __init__(self, output: Path):
        output.mkdir(parents=True, exist_ok=True)
        self.handle = (output / "run.lock").open("a+")
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            raise Agent2Error("ALREADY_RUNNING", "Classroom Enrichment is already running")

    def close(self):
        if not self.handle.closed:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()


def is_running(output: Path):
    # Status reads must not create output directories or launch work.
    if not (output / "run.lock").exists():
        return False
    try:
        with (output / "run.lock").open("r") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True


class EnrichmentRun:
    def __init__(self, adapter: ASRAdapter, output_dir: Path, provider, spec: ModelSpec):
        require(bool(spec.context_window_tokens) and spec.context_window_tokens > 0
                and bool(spec.max_output_tokens) and spec.max_output_tokens > 0,
                "Enrichment context window and output limit must be positive")
        self.adapter, self.output, self.provider, self.spec = adapter, output_dir, provider, spec
        self.prompts = {stage: (PROMPTS / f"{stage}.md").read_text(encoding="utf-8")
                        for stage in ("mapping", "retrieval", "enrichment")}
        self.binding = {**adapter.version, "prompt_fingerprint": _fingerprint(self.prompts),
                        "model_config_fingerprint": _fingerprint(spec.__dict__), "policy": "source_only_v1",
                        "output_contract_version": CONTRACT_VERSION}
        self.fingerprint = _fingerprint(self.binding)
        self.directory = output_dir / "runs" / self.fingerprint
        self.manifest = {"schema_version": "1.0", **self.binding, "input_fingerprint": self.fingerprint,
            "status": "processing", "model": spec.model, "provider": spec.provider,
            "block_count": len(adapter.blocks), "completed_block_count": 0,
            "node_count": 0, "completed_node_count": 0, "failed_block_ids": [], "failed_node_ids": [],
            "review_count": 0, "unmatched_count": 0, "requirement_count": 0,
            "stage": "mapping", "block_stages": {}, "supplemental_search_limit": 1,
            "coverage_check": "identified_fragments_and_source_ranges_only",
            "visual_policy": "extracted_text_only_review_unobserved_images"}
        self.errors = []
        self.sent_pages = set()
        self.results, self.nodes = {}, {}
        # Preserve readable progress on retries while validated stage caches are replayed.
        previous = self.directory / "mapping_blocks.jsonl"
        if previous.exists():
            try:
                for row in read_jsonl(previous):
                    if row.get("version") == self.binding:
                        block = next(b for b in adapter.blocks if b["block_id"] == row["block_id"])
                        for f in row["result"]["fragments"]:
                            ranges = [{k: s[k] for k in ("unit_id", "start", "end")} for s in f["sources"]]
                            require(adapter.sources(block, ranges) == f["sources"], "Saved fragment source mismatch")
                        self.results[row["block_id"]] = row["result"]
                        self.manifest["block_stages"][row["block_id"]] = "completed" if row["completed"] else "failed"
            except (ValueError, KeyError, TypeError, StopIteration):
                self.results = {}; self.manifest["block_stages"] = {}
        previous_nodes = self.directory / "node_enrichment.jsonl"
        if previous_nodes.exists() and self.results:
            try:
                self.nodes = {row["node_id"]: row for row in read_jsonl(previous_nodes) if row.get("version") == self.binding}
            except (ValueError, KeyError, TypeError):
                self.nodes = {}
        previous_errors = self.directory / "errors.jsonl"
        if previous_errors.exists():
            self.errors = read_jsonl(previous_errors)
        self.manifest.update(completed_block_count=sum(s == "completed" for s in self.manifest["block_stages"].values()),
            node_count=len({l["node_id"] for r in self.results.values() for l in r["relations"]}),
            completed_node_count=len(self.nodes))

    def save(self):
        self.manifest["incomplete_block_ids"] = [b["block_id"] for b in self.adapter.blocks
            if self.manifest["block_stages"].get(b["block_id"]) != "completed"]
        self.manifest["incomplete_node_ids"] = [n for n in self.manifest.get("node_ids", [])
            if n not in self.nodes or n in self.manifest["failed_node_ids"]]
        _write_json(self.directory / "manifest.json", self.manifest)
        _write_json(self.output / "manifest.json", self.manifest)

    def error(self, identity, stage, exc):
        error = {"identity": identity, "stage": stage, "error_type": getattr(exc, "error_type", type(exc).__name__),
                 "error_message": str(exc) or "Enrichment execution was interrupted."}
        self.errors.append(error)
        self.manifest["root_cause"] = error
        _write_jsonl(self.directory / "errors.jsonl", self.errors)

    def packed(self, payload):
        """Share canonical page bodies across requests; keep explicit metadata refs.

        These are references, not claims that a stateless model remembers another
        request. Full nodes and all target speech stay in each relevant request.
        """
        value = copy.deepcopy(payload)
        for owner in [value, value.get("materials", {})]:
            if "pages" not in owner:
                continue
            pages = owner["pages"]
            owner["pages"] = [p for p in pages if p["page"] not in self.sent_pages]
            owner["page_refs"] = [{"page": p["page"], "visual_required": p.get("visual_required", False),
                "body_available_in_this_request": p["page"] not in self.sent_pages,
                "source_fingerprint": self.adapter.evidence.fingerprint} for p in pages]
        return value

    def request_budget(self, stage, payload):
        # Conservative upper bound: each UTF-8 byte counts as a token. Nothing is silently truncated.
        packed, schema = self.request_payload(stage, payload)
        schema_bytes = len(_json_bytes(schema)) if schema and self.spec.provider == "codex" else 0
        return len(self.prompts[stage].encode()) + len(_json_bytes(packed)) + schema_bytes + 1024 + self.spec.max_output_tokens

    def fits(self, stage, payload):
        return self.request_budget(stage, payload) <= self.spec.context_window_tokens

    def request_payload(self, stage, payload):
        packed = self.packed(payload)
        schema = output_schema(stage, payload)
        if self.spec.provider != "codex":
            contract = output_contract(stage, payload)
            if contract:
                packed["output_contract"] = contract
        return packed, schema

    async def call(self, stage, payload):
        if not self.fits(stage, payload):
            raise Agent2Error("CONTEXT_LIMIT_EXCEEDED", f"{stage} input exceeds configured context budget")
        packed, schema = self.request_payload(stage, payload)
        kwargs = {"model": self.spec.model, "messages": [
            {"role": "system", "content": self.prompts[stage]},
            {"role": "user", "content": _json_bytes(packed).decode()}],
            "response_format": {"type": "json_object"}, "max_tokens": self.spec.max_output_tokens}
        if self.spec.provider == "codex" and schema:
            kwargs["response_format"] = {"type": "json_schema", "json_schema": {
                "name": f"classroom_{stage}", "strict": True, "schema": schema}}
        if self.spec.provider == "alibaba_cloud_model_studio":
            kwargs.update(stream=True, extra_body={"enable_thinking": False})
        for attempt in range(3):
            try:
                response = await self.provider.create_chat_completion(**kwargs)
                if hasattr(response, "__aiter__"):
                    parts = []
                    async for chunk in response:
                        for choice in getattr(chunk, "choices", []):
                            if getattr(choice, "finish_reason", None) == "length":
                                raise Agent2Error("OUTPUT_LIMIT_EXCEEDED", "Model output was truncated")
                            content = getattr(getattr(choice, "delta", None), "content", None)
                            if content:
                                parts.append(content)
                    response = "".join(parts)
                else:
                    choices = response.get("choices", []) if isinstance(response, dict) else getattr(response, "choices", [])
                    for choice in choices:
                        finish = choice.get("finish_reason") if isinstance(choice, dict) else getattr(choice, "finish_reason", None)
                        if finish == "length":
                            raise Agent2Error("OUTPUT_LIMIT_EXCEEDED", "Model output was truncated")
                value = _parse_json(_response_text(response))
                for owner in [packed, packed.get("materials", {})]:
                    self.sent_pages.update(p["page"] for p in owner.get("pages", []))
                return value
            except Exception as exc:
                if attempt == 2 or not _retryable_provider_error(exc):
                    raise
                self.error("provider", f"{stage}_retry", exc)
                await asyncio.sleep(min(_retry_delay(attempt), 10))

    async def cached(self, stage, payload, validator):
        # Full artifact digest binds the published run; per-stage payloads bind affected
        # blocks/nodes. Unchanged blocks can reuse raw model results if only another
        # corrected block changed under the same upstream run identity.
        cache_binding = {k: v for k, v in self.binding.items() if k != "asr_artifact_fingerprint"}
        key = _fingerprint({"binding": cache_binding, "stage": stage, "payload": payload})
        path = self.output / "cache" / stage / f"{key}.json"
        if path.exists():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                require(saved["input_fingerprint"] == key, "Invalid cache identity")
                validator(saved["result"])
                return saved["result"]
            except (ValueError, KeyError, TypeError):
                pass  # Corrupted cache cannot become a production result.
        value = await self.call(stage, payload)
        # A schema repair is bounded; it never reruns ASR.
        try:
            validator(value)
        except (ValueError, KeyError, TypeError) as exc:
            repair = {**payload, "validation_error": str(exc), "invalid_output": value,
                      "repair_instruction": "Return repaired JSON only. The invalid_output is a rejected draft, "
                      "not a schema example. Check the reported constraint in every fragment/item, "
                      "not only its first occurrence. Preserve exact source ranges and supported content."}
            if stage == "mapping":
                repair["mapping_contract"] = {
                    "types": {"json_type": "array", "allowed_values": sorted(TYPES),
                              "nonknowledge": "Use status=nonknowledge and types=[]; never use nonknowledge as a type."},
                    "status": {"allowed_values": sorted(STATUSES)},
                    "links": {"mapped": "At least one supported link to a node in materials.nodes.",
                              "unmatched_or_needs_review_or_nonknowledge": "Use links=[]."},
                }
            value = await self.call(stage, repair)
            validator(value)
        _write_json(path, {"input_fingerprint": key, "result": value})
        return value

    def validate_mapping(self, value, block, units, materials):
        require(isinstance(value, dict) and isinstance(value.get("fragments"), list), "Expected fragments list")
        allowed_units = {u["unit_id"] for u in units}
        allowed_nodes = {n["node_id"] for n in materials["nodes"]}
        for f in value["fragments"]:
            require(isinstance(f, dict) and f.get("status") in STATUSES, "Invalid fragment status")
            text(f.get("description")); text(f.get("reason"))
            fragment_types = f.get("types")
            require(isinstance(fragment_types, list)
                    and all(isinstance(kind, str) and kind in TYPES for kind in fragment_types),
                    f"Invalid fragment types {fragment_types!r}. types must be a list containing only "
                    f"{', '.join(sorted(TYPES))}. nonknowledge is a status, not a type; "
                    "use types=[] for filler with no teaching contribution.")
            refs = self.adapter.sources(block, f.get("ranges"))
            source = "\n".join(r["quote"] for r in refs)
            source_language_only(f["description"], source)
            source_language_only(f["reason"], source)
            require(f.get("uncertainty") is None or isinstance(f["uncertainty"], str), "Invalid fragment uncertainty")
            require(all(r["unit_id"] in allowed_units for r in refs), "Context-only unit cannot be cited")
            require(type(f.get("assessment")) is bool, "Assessment must be explicit")
            if f["assessment"] or "assessment" in f["types"]:
                require(f["status"] != "nonknowledge" and f["assessment"] and "assessment" in f["types"],
                        "Assessment requirements cannot be discarded as nonknowledge")
            links = f.get("links")
            require(isinstance(links, list) and (bool(links) == (f["status"] == "mapped")),
                    f"Status/link mismatch: status={f['status']!r}, links={links!r}. "
                    "mapped requires at least one link to a node in materials.nodes; "
                    "unmatched, needs_review and nonknowledge require links=[]. "
                    "Reassess the classification using source evidence; do not invent a node link.")
            linked = set()
            for link in links:
                require(isinstance(link, dict), "Invalid relation shape")
                require(link.get("node_id") in allowed_nodes and link["node_id"] not in linked,
                        "Unknown, unselected or duplicate node ID")
                linked.add(link["node_id"])
                for key in ("reason", "contribution", "angle"):
                    text(link.get(key))
                    source_language_only(link[key], source)
            gap = f.get("gap")
            if gap is not None:
                require(isinstance(gap, dict), "Invalid gap shape")
                require(gap.get("trigger") in {"important_uncovered", "corrected_term", "material_mismatch"}, "Invalid retrieval trigger")
                text(gap.get("query"))

    def normalize(self, value, block, units):
        fragments, relations = [], []
        covered = {u["unit_id"]: set() for u in units}
        for f in value["fragments"]:
            refs = self.adapter.sources(block, f["ranges"])
            identity_ranges = [{k: r[k] for k in ("unit_id", "start", "end")} for r in refs]
            fragment_id = f"{self.adapter.identity['session_id']}:{block['block_id']}:{_fingerprint(identity_ranges)[:20]}"
            corrections = [e for e in block["corrections"] if e["unit_id"] in {r["unit_id"] for r in refs}]
            unresolved = [e["correction_id"] for e in corrections if e["decision"] == "flag"]
            fragment = {"fragment_id": fragment_id, "block_id": block["block_id"],
                "block_range": [block["start_unit_id"], block["end_unit_id"]],
                "description": f["description"], "types": f["types"], "status": f["status"],
                "reason": f["reason"], "assessment": f["assessment"], "sources": refs,
                "corrected_text": "\n".join(r["quote"] for r in refs), "corrections": corrections,
                "unresolved_correction_ids": unresolved, "gap": f.get("gap")}
            fragment["uncertainty"] = f.get("uncertainty")
            fragments.append(fragment)
            for r in refs:
                covered[r["unit_id"]].update(range(r["start"], r["end"]))
            relations.extend({**link, "fragment_id": fragment_id, "types": f["types"]} for link in f["links"])
        # Coverage is structural, not proof of semantic completeness. Preserve every missed character visibly.
        for unit in units:
            missing = sorted(set(range(len(unit["text"]))) - covered[unit["unit_id"]])
            spans = []
            for pos in missing:
                if spans and spans[-1]["end"] == pos:
                    spans[-1]["end"] += 1
                else:
                    spans.append({"unit_id": unit["unit_id"], "start": pos, "end": pos + 1})
            spans = [s for s in spans if unit["text"][s["start"]:s["end"]].strip()]
            if spans:
                refs = self.adapter.sources(block, spans)
                assessment = bool(re.search(r"\b(exam|assignment|homework|project|assessment|coursework|quiz)\b|考试|作业|考核|项目要求",
                                            unit["text"], re.IGNORECASE))
                fragments.append({"fragment_id": f"{self.adapter.identity['session_id']}:{block['block_id']}:uncovered:{_fingerprint(spans)[:20]}",
                    "block_id": block["block_id"], "block_range": [block["start_unit_id"], block["end_unit_id"]],
                    "description": "\n".join(r["quote"] for r in refs), "corrected_text": "\n".join(r["quote"] for r in refs),
                    "types": ["assessment"] if assessment else [], "status": "needs_review", "reason": "Source range was not classified by the mapping model.",
                    "assessment": assessment, "sources": refs, "corrections": [], "unresolved_correction_ids": [],
                    "uncertainty": "Source range classification requires review.", "gap": None, "review_code": "UNCOVERED_SOURCE"})
        require(len({f["fragment_id"] for f in fragments}) == len(fragments), "Duplicate fragment ranges")
        return {"fragments": fragments, "relations": relations}

    def windows(self, block, materials, exhausted):
        base = {"block_id": block["block_id"], "corrected_summary": block["summary"],
                "evidence_provenance": EVIDENCE_PROVENANCE,
                "corrections": block["corrections"], "materials": materials,
                "validation_issues": [{k: issue.get(k) for k in ("unit_id", "edit_index", "action", "error_message")}
                                      for issue in block.get("validation_issues", [])],
                "supplemental_search_exhausted": exhausted}
        units = [{"unit_id": u["unit_id"], "text": u["text"], "char_length": len(u["text"])}
                 for u in block["units"]]
        def divide(start, end):
            payload = {**base, "window_id": f"{block['block_id']}:{units[start]['unit_id']}:{units[end-1]['unit_id']}",
                       "target_units": units[start:end], "context_units": units[max(0, start-1):start] + units[end:end+1]}
            if self.fits("mapping", payload):
                return [(payload, units[start:end])]
            if end - start <= 1:
                raise Agent2Error("CONTEXT_LIMIT_EXCEEDED", "Single unit/material package exceeds budget; no source was truncated")
            middle = (start + end) // 2
            return divide(start, middle) + divide(middle, end)
        return divide(0, len(units))

    async def map_units(self, block, materials, exhausted):
        output = {"fragments": [], "relations": []}
        for payload, units in self.windows(block, materials, exhausted):
            value = await self.cached("mapping", payload,
                lambda v: self.validate_mapping(v, block, units, materials))
            normalized = self.normalize(value, block, units)
            for key in output:
                output[key].extend(normalized[key])
        return output

    async def locate(self, block, gaps, materials):
        payload = {"block_id": block["block_id"], "gaps": gaps, "indexes": self.adapter.index(),
                   "already_supplied_node_ids": [n["node_id"] for n in materials["nodes"]],
                   "already_supplied_pages": [p["page"] for p in materials["pages"]]}
        selection = await self.cached("retrieval", payload, self.adapter.evidence.validate_selection)
        extra = self.adapter.evidence.selected_evidence(selection)
        return {key: list({row["node_id" if key == "nodes" else "page"]: row
                         for row in materials[key] + extra[key]}.values()) for key in ("nodes", "pages")}

    async def map_block(self, block):
        materials = block["materials"]
        searches = 0
        if not block["candidate_metadata_available"]:
            materials = await self.locate(block, [{"trigger": "missing_metadata",
                "text": "\n".join(u["text"] for u in block["units"])}], materials)
            searches = 1
        result = await self.map_units(block, materials, bool(searches))
        result["supplemental_search_count"] = searches
        # Keep already completed mapping evidence if a subsequent gap request fails.
        self.results[block["block_id"]] = copy.deepcopy(result)
        gaps = [f for f in result["fragments"] if f.get("gap")]
        if gaps and not searches:
            self.results[block["block_id"]]["supplemental_search_count"] = 1
            previous_pages = {p["page"] for p in materials["pages"]}
            previous_nodes = {n["node_id"] for n in materials["nodes"]}
            materials = await self.locate(block, [{"fragment_id": f["fragment_id"], "gap": f["gap"],
                "text": f["corrected_text"]} for f in gaps], materials)
            searches = 1
            result["supplemental_search_count"] = searches
            gap_materials = {"nodes": [n for n in materials["nodes"] if n["node_id"] not in previous_nodes],
                             "pages": [p for p in materials["pages"] if p["page"] not in previous_pages]}
            # Existing nodes can benefit from a newly located page; otherwise only
            # new nodes need reconsideration. Previously sent pages are never repeated.
            if not gap_materials["nodes"]:
                gap_materials["nodes"] = materials["nodes"]
            # Only re-map gap source ranges. Successful fragments are preserved.
            # Restrict target text with an allowed-ranges contract to avoid recreating mapped neighbors.
            for f in gaps:
                subset = {**block, "units": [u for u in block["units"] if u["unit_id"] in {r["unit_id"] for r in f["sources"]}]}
                payload = {"block_id": block["block_id"], "window_id": f["fragment_id"],
                    "target_units": [{"unit_id": u["unit_id"], "text": u["text"], "char_length": len(u["text"])}
                                     for u in subset["units"]],
                    "allowed_ranges": [{k: r[k] for k in ("unit_id", "start", "end")} for r in f["sources"]],
                    "context_units": [], "materials": gap_materials, "corrections": f["corrections"],
                    "supplemental_search_exhausted": True,
                    "instruction": "Only classify allowed_ranges; do not cite other characters of target_units."}
                def check(v):
                    self.validate_mapping(v, block, subset["units"], gap_materials)
                    for item in v["fragments"]:
                        for r in item["ranges"]:
                            require(any(r["unit_id"] == s["unit_id"] and s["start"] <= r["start"] < r["end"] <= s["end"]
                                        for s in f["sources"]), "Supplemental mapping exceeded gap range")
                value = await self.cached("mapping", payload, check)
                # Normalize only the allowed range; discard coverage checks outside this gap.
                normalized = self.normalize(value, block, subset["units"])
                replacement = [x for x in normalized["fragments"] if x.get("review_code") != "UNCOVERED_SOURCE"]
                covered = {(r["unit_id"], p) for x in replacement for r in x["sources"] for p in range(r["start"], r["end"])}
                expected = {(r["unit_id"], p) for r in f["sources"] for p in range(r["start"], r["end"])}
                if covered >= expected and replacement:
                    result["fragments"] = [x for x in result["fragments"] if x["fragment_id"] != f["fragment_id"]] + replacement
                    result["relations"] = [x for x in result["relations"] if x["fragment_id"] != f["fragment_id"]] + normalized["relations"]
                    require(len({x["fragment_id"] for x in result["fragments"]}) == len(result["fragments"]), "Duplicate fragment identity")
                    self.results[block["block_id"]] = copy.deepcopy(result)
                else:
                    f["status"] = "needs_review" if f["status"] != "mapped" else "mapped"
                    f["review_code"] = "INCOMPLETE_GAP_MAPPING"
        for f in result["fragments"]:
            if f.get("gap"):
                f["review_code"] = "SEARCH_LIMIT_REACHED"
        result["supplemental_search_count"] = searches
        require(len({f["fragment_id"] for f in result["fragments"]}) == len(result["fragments"]), "Duplicate fragment identity")
        return result

    def aggregate(self):
        fragments = [f for r in self.results.values() for f in r["fragments"]]
        relations = [l for r in self.results.values() for l in r["relations"]]
        ids = {f["fragment_id"]: f for f in fragments}
        require(len(ids) == len(fragments), "Fragment IDs must be unique")
        for f in fragments:
            links = [l for l in relations if l["fragment_id"] == f["fragment_id"]]
            require(bool(links) == (f["status"] == "mapped"), "Every identified fragment must have a consistent destination")
        require(all(l["fragment_id"] in ids and l["node_id"] in self.adapter.evidence.scaffold["nodes"] for l in relations), "Invalid relation identity")
        shared = [{"shared_content_id": f"shared:{f['fragment_id']}", "fragment_id": f["fragment_id"],
                   "description": f["description"], "text": f["corrected_text"], "sources": f["sources"]}
                  for f in fragments if len({l["node_id"] for l in relations if l["fragment_id"] == f["fragment_id"]}) > 1
                  and set(f["types"]) & {"example", "reasoning", "procedure"}]
        pending = [{**f, "record_type": "new_node_candidate" if f["status"] == "unmatched" else "review"}
                   for f in fragments if f["status"] in {"unmatched", "needs_review"} or f["unresolved_correction_ids"] or f.get("review_code") or f.get("uncertainty")]
        requirements = [f for f in fragments if f["assessment"]]
        return fragments, relations, shared, pending, requirements

    def publish(self):
        fragments, relations, shared, pending, requirements = self.aggregate()
        for name, rows in (("fragments", fragments), ("relations", relations), ("shared_content", shared),
                           ("pending", pending), ("requirements", requirements), ("node_enrichment", list(self.nodes.values()))):
            _write_jsonl(self.directory / f"{name}.jsonl", rows)
        _write_jsonl(self.directory / "mapping_blocks.jsonl", [{"block_id": key, "version": self.binding,
            "completed": self.manifest["block_stages"].get(key) == "completed", "result": value}
            for key, value in self.results.items()])
        self.manifest.update(completed_block_count=len(self.results), completed_node_count=len(self.nodes),
            review_count=sum(r["record_type"] == "review" for r in pending),
            unmatched_count=sum(f["status"] == "unmatched" for f in fragments), requirement_count=len(requirements),
            supplemental_search_counts={k: r["supplemental_search_count"] for k, r in self.results.items()})
        self.manifest["completed_block_count"] = sum(s == "completed" for s in self.manifest["block_stages"].values())
        self.manifest["incomplete_block_ids"] = [b["block_id"] for b in self.adapter.blocks
            if self.manifest["block_stages"].get(b["block_id"]) != "completed"]
        self.manifest["node_review_count"] = sum(i["needs_review"] for n in self.nodes.values() for i in n["items"])
        self.manifest["review_count"] += self.manifest["node_review_count"]
        self.save()

    def validate_enrichment(self, value, payload):
        expected_node = payload["node"]["node_id"]
        received_node = value.get("node_id") if isinstance(value, dict) else None
        require(isinstance(value, dict) and received_node == expected_node,
                f"Wrong enrichment node: expected node_id={expected_node}, received {received_node!r}. "
                "Copy the node_id from the input node exactly; do not use a child, parent or example ID.")
        require(isinstance(value.get("items"), list) and isinstance(value.get("omitted_fragments"), list), "Invalid enrichment shape")
        allowed = {f["fragment_id"] for f in payload["fragments"]}
        shared = {s["shared_content_id"]: s["fragment_id"] for s in payload["shared_content"]}
        pages = {p["page"]: p["text"] for p in payload["pages"]}
        scaffold_text = "\n".join(material_strings({"content": payload["node"]["content"],
                "evidence": payload["node"]["evidence"], "title": payload["node"]["title"]}))
        used = set()
        for item in value["items"]:
            require(isinstance(item, dict), "Invalid enrichment item")
            text(item.get("heading")); text(item.get("text"))
            refs = item.get("fragment_ids")
            require(isinstance(refs, list) and bool(refs) and set(refs) <= allowed, "Item needs real fragment citations")
            used.update(refs)
            source = "\n".join(f["corrected_text"] for f in payload["fragments"] if f["fragment_id"] in refs)
            source_language_only(item["heading"] + " " + item["text"], source)
            require(item.get("source_nature") in NATURES, "Invalid source nature")
            require(isinstance(item.get("shared_content_ids"), list)
                    and all(s in shared and shared[s] in refs for s in item["shared_content_ids"]), "Invalid shared content reference")
            relevant_shared = [s for s in payload["shared_content"] if s["fragment_id"] in refs]
            require(all(s["shared_content_id"] in item["shared_content_ids"] for s in relevant_shared),
                    "A shared example must be referenced by its canonical identity")
            require(all(s["text"] not in item["text"] for s in relevant_shared),
                    "Shared example was copied instead of describing the node-specific angle")
            require(isinstance(item.get("course_evidence"), list), "Invalid course evidence")
            for e in item["course_evidence"]:
                require(isinstance(e, dict), "Invalid course evidence shape")
                require(e.get("source", "slide") in {"slide", "scaffold"}, "Unknown course evidence source")
                context = scaffold_text if e.get("source") == "scaffold" else pages.get(e.get("page"), "")
                require(type(e.get("page")) is int and e["page"] in pages and text(e.get("quote")) in context,
                        f"Invented course evidence: page={e.get('page')!r}, source={e.get('source', 'slide')!r}, "
                        f"quote={e.get('quote')!r}; allowed pages={sorted(pages)}. "
                        "Copy a literal quote from the current node for source=scaffold, or a supplied page body for source=slide. "
                        "Do not paraphrase quotations or assume unavailable page bodies. If exact course evidence is unavailable, "
                        "retain the classroom fragment with source_nature=uncertain and course_evidence=[] rather than invent a citation.")
            if item["source_nature"] == "explaining_course_material":
                require(bool(item["course_evidence"]), "Course explanation needs page evidence")
            if item["source_nature"] == "lecturer_addition":
                text(item.get("addition_basis"))
            require(item.get("uncertainty") is None or isinstance(item["uncertainty"], str), "Invalid uncertainty")
        for omitted in value["omitted_fragments"]:
            require(isinstance(omitted, dict), "Invalid omitted fragment shape")
            require(omitted.get("fragment_id") in allowed, "Unknown omitted fragment")
            text(omitted.get("reason")); used.add(omitted["fragment_id"])
        require(used == allowed, "Enrichment silently omitted input fragments")

    async def enrich_node(self, node_id, fragments, relations, shared):
        node = self.adapter.evidence.scaffold["nodes"][node_id]
        links = [l for l in relations if l["node_id"] == node_id]
        allowed = {l["fragment_id"] for l in links}
        selected = [f for f in fragments if f["fragment_id"] in allowed]
        pages = self.adapter.evidence.selected_evidence({"node_matches": [{"node_id": node_id, "reason": "Node context"}],
            "page_matches": [], "unmatched_reason": None})["pages"]
        base = {"node": node, "pages": pages, "evidence_provenance": EVIDENCE_PROVENANCE}
        def payload(batch):
            ids = {f["fragment_id"] for f in batch}
            # Strip expanded sources from model input; resolved references remain in persisted output.
            return {**base, "fragments": [{k: f[k] for k in ("fragment_id", "description", "corrected_text", "types", "corrections", "unresolved_correction_ids", "uncertainty")} for f in batch],
                    "relations": [l for l in links if l["fragment_id"] in ids],
                    "shared_content": [{k: s[k] for k in ("shared_content_id", "fragment_id", "description", "text")} for s in shared if s["fragment_id"] in ids]}
        batches, batch = [], []
        for f in selected:
            if batch and not self.fits("enrichment", payload(batch + [f])):
                batches.append(batch); batch = []
            if not self.fits("enrichment", payload([f])):
                raise Agent2Error("CONTEXT_LIMIT_EXCEEDED", "Single fragment/node context exceeds enrichment budget; no source was truncated")
            batch.append(f)
        if batch:
            batches.append(batch)
        items, omitted = [], []
        for batch in batches:
            input_value = payload(batch)
            value = await self.cached("enrichment", input_value, lambda v: self.validate_enrichment(v, input_value))
            items.extend(value["items"]); omitted.extend(value["omitted_fragments"])
        # Exact duplicate integration retains every source; nonidentical statements/conflicts remain visible.
        integrated = {}
        for item in items:
            key = _fingerprint({k: item.get(k) for k in ("heading", "text", "source_nature", "uncertainty", "addition_basis")})
            if key in integrated:
                current = integrated[key]
                for field in ("fragment_ids", "shared_content_ids", "course_evidence"):
                    current[field] = list({_fingerprint(x): x for x in current[field] + item[field]}.values())
            else:
                integrated[key] = {**item}
        for item in integrated.values():
            evidence_fragments = [f for f in selected if f["fragment_id"] in item["fragment_ids"]]
            item["sources"] = list({_fingerprint(r): r for f in evidence_fragments for r in f["sources"]}.values())
            item["unresolved_correction_ids"] = list({c for f in evidence_fragments for c in f["unresolved_correction_ids"]})
            unresolved = [f["uncertainty"] for f in evidence_fragments if f.get("uncertainty")]
            if unresolved:
                item["uncertainty"] = "\n".join(dict.fromkeys([item["uncertainty"] or "", *unresolved])).strip()
            item["needs_review"] = bool(item["uncertainty"] or item["unresolved_correction_ids"]
                                        or item["source_nature"] == "uncertain" or len(batches) > 1)
            if item["unresolved_correction_ids"] and not item["uncertainty"]:
                item["uncertainty"] = "Supporting classroom evidence has unresolved ASR questions."
            elif len(batches) > 1 and not item["uncertainty"]:
                item["uncertainty"] = "Organized in separate batches; cross-batch repetitions and conflicts require review."
            elif item["source_nature"] == "uncertain" and not item["uncertainty"]:
                item["uncertainty"] = "The relationship between this classroom contribution and course material is uncertain."
            item["item_id"] = f"{node_id}:{_fingerprint(item)[:20]}"
        return {"node_id": node_id, "items": list(integrated.values()), "omitted_fragments": omitted,
                "batch_count": len(batches), "integration_policy": "model_within_budget_exact_merge_across_batches",
                "fragment_ids": sorted(allowed), "version": self.binding}

    async def run(self, lease=None):
        lease = lease or RunLease(self.output)
        try:
            self.save()
            for block in self.adapter.blocks:
                block_id = block["block_id"]
                self.manifest["block_stages"][block_id] = "mapping"; self.save()
                try:
                    self.results[block_id] = await self.map_block(block)
                    self.manifest["block_stages"][block_id] = "completed"
                except Exception as exc:
                    self.error(block_id, "mapping", exc)
                    self.manifest["failed_block_ids"].append(block_id)
                    self.manifest["block_stages"][block_id] = "failed"
                    if getattr(exc, "error_type", None) == "CODEX_USAGE_LIMIT":
                        self.publish()
                        raise
                self.publish()
            fragments, relations, shared, _, _ = self.aggregate()
            node_ids = sorted({l["node_id"] for l in relations})
            self.manifest.update(node_count=len(node_ids), node_ids=node_ids, stage="enrichment"); self.save()
            for node_id in node_ids:
                try:
                    self.nodes[node_id] = await self.enrich_node(node_id, fragments, relations, shared)
                except Exception as exc:
                    self.error(node_id, "enrichment", exc)
                    self.manifest["failed_node_ids"].append(node_id)
                    if getattr(exc, "error_type", None) == "CODEX_USAGE_LIMIT":
                        self.publish()
                        raise
                self.publish()
            self.manifest.update(status="completed_with_errors" if self.manifest["failed_block_ids"] or self.manifest["failed_node_ids"] else "completed", stage="finished")
            if not self.results:
                self.manifest["status"] = "failed"
            if self.manifest["status"] == "completed":
                self.manifest.pop("root_cause", None)
            self.publish()
        except BaseException as exc:
            self.error("run", self.manifest["stage"], exc)
            self.manifest["status"] = "failed"; self.save()
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            lease.close()
        return self.manifest


def read_results(output, manifest):
    fp = manifest.get("input_fingerprint", "")
    require(isinstance(fp, str) and re.fullmatch(r"[0-9a-f]{64}", fp), "Invalid enrichment run identity")
    directory = output / "runs" / fp
    return {name: read_jsonl(directory / f"{name}.jsonl") if (directory / f"{name}.jsonl").exists() else []
            for name in ("fragments", "relations", "shared_content", "pending", "requirements", "node_enrichment")}
