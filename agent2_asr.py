"""Post-class Agent2 evidence retrieval and audited ASR correction.

Original Agent1 and PreAgent artifacts are read-only. Published results are
versioned by input fingerprint, with per-stage caches for interrupted runs.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import unicodedata
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from agent1_v1 import (Agent1V1Error, ChatProvider, ModelSpec, _fingerprint, _json_bytes, _parse_json,
    _response_text, _retry_delay, _retryable_provider_error, _write_json, _write_jsonl,
    build_units, load_final_source, normalize_transcript, validate_segmentation, validate_summary)
from agent2_retrieval import Agent2Error, EVIDENCE_PROVENANCE, LectureEvidence, read_jsonl

PROMPT_ROOT = Path(__file__).resolve().parent / "agent2_prompts"
SUMMARY_PROMPT = PROMPT_ROOT / "summary_v1.md"
POLICY = "agent2_scoped_uncertainty_v3"
HIGH_IMPACT = {"number", "formula", "unit", "factual"}


def _local_correction_tails(edit: dict[str, Any], units: list[dict[str, str]]) -> list[str]:
    """Bound explicit correction cues to the edited occurrence's local speech."""
    position = next(i for i, unit in enumerate(units) if unit["unit_id"] == edit["unit_id"])
    current = units[position]["text"]
    _, end = _span(current, edit["original"], edit["occurrence"])
    after = " ".join([current[end:], *(u["text"] for u in units[position + 1:position + 3])])
    return [match.group() for match in re.finditer(
        r"\b(?:sorry|correction|I mean)\b(?:\.(?=\d)|[^.!?\n]){0,100}", after[:200], re.I)]


def _explicit_numeric_correction(edit: dict[str, Any], units: list[dict[str, str]]) -> bool:
    """Another value in a long block is not proof of a recognition error."""
    expected = {n.replace(",", "") for n in re.findall(r"\d+(?:[.,]\d+)*", edit["replacement"])}
    if not expected:
        return False
    for tail in _local_correction_tails(edit, units):
        stated = {n.replace(",", "") for n in re.findall(r"\d+(?:[.,]\d+)*", tail)}
        if expected <= stated:
            return True
    return False


def _evidence_supports(replacement: str, quote: str, kind: str) -> bool:
    """Allow spelling layout differences in terms, never numeric equivalence.

    Token boundaries prevent matching rdt2.0 inside rdt12.0 or 'timer' inside
    'timers'. Operators, underscores, units and decimal values stay significant.
    """
    def tokens(text: str) -> list[str]:
        text = unicodedata.normalize("NFKC", text).casefold()
        text = text.translate(str.maketrans({"‐": "-", "‑": "-", "–": "-", "’": "'", "“": '"', "”": '"'}))
        return re.findall(r"[^\W\d_]+|\d+(?:[.,]\d+)*|[^\s]", text)

    if kind != "terminology":
        term = " ".join(replacement.casefold().split())
        pattern = (r"(?<!\w)" if term and re.match(r"\w", term[0]) else "") + re.escape(term)
        pattern += r"(?!\w)" if term and re.match(r"\w", term[-1]) else ""
        return bool(term) and re.search(pattern, " ".join(quote.casefold().split())) is not None
    term, evidence = tokens(replacement), tokens(quote)
    # Sentence punctuation is not part of a term. Code parentheses remain.
    while term and term[-1] in {".", ",", ";", ":", "!", "?"}:
        term.pop()
    return bool(term) and any(evidence[i:i + len(term)] == term for i in range(len(evidence) - len(term) + 1))


def _span(text: str, original: str, occurrence: int) -> tuple[int, int]:
    start = -len(original)
    for _ in range(occurrence):
        start = text.find(original, start + len(original))
        if start < 0:
            raise Agent2Error("INVALID_CORRECTION",
                f"Original span {original!r}, occurrence {occurrence}, does not exist in this unit's exact source text: {text!r}. Check unit_id and copy original verbatim, preserving punctuation and spacing.")
    return start, start + len(original)


def validate_corrections(value: Any, *, block_id: str, units: list[dict[str, str]],
                         evidence: dict[str, Any], page_ids: set[int], expanded: bool) -> None:
    if not isinstance(value, dict) or set(value) != {"block_id", "edits", "additional_pages"} or value["block_id"] != block_id:
        raise Agent2Error("INVALID_CORRECTION", "Correction response has invalid fields or block_id")
    extra = value["additional_pages"]
    if (not isinstance(extra, list) or any(type(page) is not int or page not in page_ids for page in extra)
            or len(set(extra)) != len(extra) or (expanded and extra)):
        raise Agent2Error("INVALID_CORRECTION", "Invalid additional page request")
    pages = {page["page"]: page["text"] for page in evidence["pages"]}
    if set(extra) & pages.keys():
        raise Agent2Error("INVALID_CORRECTION", "Additional pages must add new evidence")
    source = {unit["unit_id"]: unit["text"] for unit in units}
    transcript = " ".join(source.values())
    if not isinstance(value["edits"], list):
        raise Agent2Error("INVALID_CORRECTION", "edits must be a list")
    # Model selects stable source references; code copies exact quotation text.
    # Previously resolved references remain valid for cache reads and audits.
    segments = {(page["page"], segment["evidence_id"]): segment["text"]
                for page in evidence["pages"] for segment in page.get("segments", [])}
    unit_positions = {unit["unit_id"]: i for i, unit in enumerate(units)}
    for edit in value["edits"]:
        if not isinstance(edit, dict):
            raise Agent2Error("INVALID_CORRECTION", "Each edit must be an object")
        refs = edit.get("evidence")
        if isinstance(refs, list):
            for i, ref in enumerate(refs):
                if isinstance(ref, dict) and set(ref) == {"page", "evidence_id"}:
                    if type(ref["page"]) is not int or not isinstance(ref["evidence_id"], str) or (ref["page"], ref["evidence_id"]) not in segments:
                        raise Agent2Error("INVALID_CORRECTION", f"Unknown supplied evidence reference: {ref}")
                    refs[i] = {"page": ref["page"], "quote": segments[(ref["page"], ref["evidence_id"])]}
        alignment = edit.get("alignment")
        if isinstance(alignment, dict) and set(alignment) == {"page", "evidence_id", "start_unit_id", "end_unit_id"}:
            if (type(alignment["page"]) is not int or not isinstance(alignment["evidence_id"], str)
                    or (alignment["page"], alignment["evidence_id"]) not in segments
                    or not isinstance(alignment["start_unit_id"], str) or alignment["start_unit_id"] not in unit_positions
                    or not isinstance(alignment["end_unit_id"], str) or alignment["end_unit_id"] not in unit_positions):
                raise Agent2Error("INVALID_CORRECTION", "Alignment references must exist in supplied block and page evidence")
            start, stop = unit_positions[alignment["start_unit_id"]], unit_positions[alignment["end_unit_id"]]
            if start > stop:
                raise Agent2Error("INVALID_CORRECTION", "Alignment unit range is reversed")
            edit["alignment"] = {"page": alignment["page"],
                "transcript_quote": " ".join(unit["text"] for unit in units[start:stop + 1]),
                "slide_quote": segments[(alignment["page"], alignment["evidence_id"])]}
    occupied: dict[str, list[tuple[int, int]]] = {}
    for edit in value["edits"]:
        fields = {"unit_id", "original", "replacement", "occurrence", "kind", "decision", "reason", "evidence", "alignment"}
        if not isinstance(edit, dict) or set(edit) != fields:
            raise Agent2Error("INVALID_CORRECTION", "An edit has missing/unknown fields")
        unit_id = edit["unit_id"]
        if not isinstance(unit_id, str) or unit_id not in source:
            raise Agent2Error("INVALID_CORRECTION", "Edit references a unit outside the block")
        if not isinstance(edit["original"], str) or not edit["original"] or type(edit["occurrence"]) is not int or not 1 <= edit["occurrence"] <= 1000:
            raise Agent2Error("INVALID_CORRECTION", "Edit needs a nonempty exact span and positive occurrence")
        try:
            start, end = _span(source[unit_id], edit["original"], edit["occurrence"])
        except Agent2Error as exc:
            raise Agent2Error("INVALID_CORRECTION", f"Edit in {unit_id}: {exc}") from exc
        if (not isinstance(edit["kind"], str) or not isinstance(edit["decision"], str)
                or edit["kind"] not in HIGH_IMPACT | {"terminology", "wording"} or edit["decision"] not in {"apply", "flag"}):
            raise Agent2Error("INVALID_CORRECTION", "Unknown correction kind/decision")
        if not isinstance(edit["reason"], str) or not edit["reason"].strip():
            raise Agent2Error("INVALID_CORRECTION", "An edit needs a reason")
        replacement = edit["replacement"]
        if replacement is not None and (not isinstance(replacement, str) or not replacement.strip()):
            raise Agent2Error("INVALID_CORRECTION", "Replacement must be null or nonempty text")
        if not isinstance(edit["evidence"], list):
            raise Agent2Error("INVALID_CORRECTION", "Evidence must be a list")
        for ref in edit["evidence"]:
            if (not isinstance(ref, dict) or set(ref) != {"page", "quote"}
                    or type(ref["page"]) is not int or ref["page"] not in pages
                    or not isinstance(ref["quote"], str) or not ref["quote"].strip()
                    or ref["quote"] not in pages[ref["page"]]):
                raise Agent2Error("INVALID_CORRECTION", "Evidence quote must exist verbatim on a supplied page")
        alignment = edit["alignment"]
        if alignment is not None:
            if (not isinstance(alignment, dict) or set(alignment) != {"page", "transcript_quote", "slide_quote"}
                    or type(alignment["page"]) is not int or alignment["page"] not in pages
                    or not isinstance(alignment["transcript_quote"], str) or not alignment["transcript_quote"].strip()
                    or alignment["transcript_quote"] not in transcript
                    or not isinstance(alignment["slide_quote"], str) or not alignment["slide_quote"].strip()
                    or alignment["slide_quote"] not in pages[alignment["page"]]):
                raise Agent2Error("INVALID_CORRECTION", "Example alignment quotes do not match source evidence")
            if any(len(re.findall(r"[A-Za-z]{2,}", alignment[key])) < 3 for key in ("transcript_quote", "slide_quote")):
                raise Agent2Error("INVALID_CORRECTION", "Alignment needs example/formula context, not just conflicting values")
        if edit["decision"] == "apply":
            if not replacement or replacement == edit["original"] or not edit["evidence"]:
                raise Agent2Error("INVALID_CORRECTION", "Applied changes need a different replacement and slide evidence")
            if not any(_evidence_supports(replacement, ref["quote"], edit["kind"]) for ref in edit["evidence"]):
                raise Agent2Error("INVALID_CORRECTION", "Replacement must be supported literally by a cited slide quote")
            # Relabeling a number as a lexical edit cannot bypass the high-impact gate.
            numbers_changed = re.findall(r"\d+(?:[.,]\d+)*", edit["original"]) != re.findall(r"\d+(?:[.,]\d+)*", replacement)
            negatives = r"\b(?:not|no|never|without|cannot)\b|n't\b"
            negation_changed = re.findall(negatives, edit["original"].casefold()) != re.findall(negatives, replacement.casefold())
            if (edit["kind"] in HIGH_IMPACT or numbers_changed or negation_changed) and alignment is None:
                raise Agent2Error("INVALID_CORRECTION", "High-impact changes need exact same-example alignment evidence")
            if alignment is not None and source[unit_id] not in alignment["transcript_quote"]:
                raise Agent2Error("INVALID_CORRECTION", "Alignment must include the edited source unit, not a different example")
            # A matching slide example alone cannot prove that changed parameters
            # were recognition errors. Require corroborating values in speech.
            spoken_numbers = {number.replace(",", "") for number in re.findall(r"\d+(?:[.,]\d+)*", transcript)}
            replacement_numbers = {number.replace(",", "") for number in re.findall(r"\d+(?:[.,]\d+)*", replacement)}
            lecturer_deviation = re.search(r"\b(?:misread|misspoke)\b|(?:lecturer|teacher)(?:'s)? (?:mistake|error)", edit["reason"], re.I)
            if lecturer_deviation or (numbers_changed and not replacement_numbers <= spoken_numbers):
                edit["decision"] = "flag"
                edit["reason"] += " Preserved for review: the slide difference lacks corroborating spoken values or may be a lecturer deviation."
                continue
            if numbers_changed and not _explicit_numeric_correction(edit, units):
                edit["decision"] = "flag"
                edit["reason"] += " Preserved for review: a conflicting value or another occurrence in the block does not establish the intended number; no local explicit spoken correction confirms it."
                continue
            if negation_changed and not any(_evidence_supports(replacement, tail, edit["kind"])
                    for tail in _local_correction_tails(edit, units)):
                edit["decision"] = "flag"
                edit["reason"] += " Preserved for review: no local explicit spoken correction confirms the changed negation."
                continue
            spans = occupied.setdefault(unit_id, [])
            if any(start < stop and end > begin for begin, stop in spans):
                raise Agent2Error("INVALID_CORRECTION", "Applied edits overlap")
            spans.append((start, end))


def resolve_corrections(value: Any, *, block_id: str, units: list[dict[str, str]],
                        evidence: dict[str, Any], page_ids: set[int], expanded: bool) -> list[dict[str, Any]]:
    """Validate the envelope, then isolate unsafe edits without changing speech.

    Every accepted edit still passes the strict validator. Rejected proposals
    are auditable; if their exact speech span exists, they become review flags.
    Unlocatable proposals are skipped while retaining every original unit.
    """
    arguments = dict(block_id=block_id, units=units, evidence=evidence, page_ids=page_ids, expanded=expanded)
    if not isinstance(value, dict) or set(value) != {"block_id", "edits", "additional_pages"} or not isinstance(value.get("edits"), list):
        raise Agent2Error("INVALID_CORRECTION", f"{block_id}: correction response has invalid fields or edits")
    envelope = {**value, "edits": []}
    try:
        validate_corrections(envelope, **arguments)
    except Agent2Error as exc:
        raise Agent2Error(exc.error_type, f"{block_id}: {exc}") from exc
    accepted: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    source = {u["unit_id"]: u["text"] for u in units}
    pages = {p["page"]: p["text"] for p in evidence["pages"]}
    segments = {(p["page"], s["evidence_id"]): s["text"] for p in evidence["pages"] for s in p.get("segments", [])}
    for index, proposal in enumerate(value["edits"], 1):
        trial = {**value, "edits": copy.deepcopy(accepted + [proposal])}
        try:
            validate_corrections(trial, **arguments)
            resolved = trial["edits"][-1]
            if proposal.get("decision") == "apply" and resolved["decision"] == "flag":
                issues.append({"block_id": block_id, "edit_index": index, "unit_id": resolved["unit_id"],
                    "action": "flagged", "error_type": "UNCERTAIN_CHANGE", "error_message": resolved["reason"],
                    "proposal": copy.deepcopy(proposal)})
            accepted = trial["edits"]
            continue
        except Agent2Error as exc:
            message, error_type = str(exc), exc.error_type
        unit_id = proposal.get("unit_id") if isinstance(proposal, dict) else None
        issue = {"block_id": block_id, "edit_index": index, "unit_id": unit_id if isinstance(unit_id, str) else None,
                 "action": "skipped", "error_type": error_type,
                 "error_message": f"{block_id}, edit {index}, {unit_id or 'unknown unit'}: {message}",
                 "proposal": copy.deepcopy(proposal)}
        # Only attach a flag to a proven exact original span. Never repair an
        # invented unit ID, occurrence or original by guessing its location.
        if isinstance(proposal, dict) and isinstance(unit_id, str) and unit_id in source:
            original, occurrence = proposal.get("original"), proposal.get("occurrence")
            if isinstance(original, str) and original and type(occurrence) is int and 1 <= occurrence <= 1000:
                try:
                    _span(source[unit_id], original, occurrence)
                    refs = []
                    for ref in proposal.get("evidence", []) if isinstance(proposal.get("evidence"), list) else []:
                        if not isinstance(ref, dict) or type(ref.get("page")) is not int:
                            continue
                        page = ref["page"]
                        quote = segments.get((page, ref.get("evidence_id"))) if isinstance(ref.get("evidence_id"), str) else ref.get("quote")
                        if isinstance(quote, str) and quote.strip() and page in pages and quote in pages[page]:
                            refs.append({"page": page, "quote": quote})
                    replacement = proposal.get("replacement")
                    if not isinstance(replacement, str) or not replacement.strip() or replacement == original:
                        replacement = None
                    reason = proposal.get("reason") if isinstance(proposal.get("reason"), str) else "Proposed correction requires review."
                    flagged = dict(unit_id=unit_id, original=original, occurrence=occurrence, replacement=replacement,
                        kind=proposal.get("kind") if isinstance(proposal.get("kind"), str) and proposal["kind"] in HIGH_IMPACT | {"terminology", "wording"} else "wording",
                        decision="flag", reason=f"{reason} Original preserved. {issue['error_message']}", evidence=refs, alignment=None)
                    check = {**value, "edits": copy.deepcopy(accepted + [flagged])}
                    validate_corrections(check, **arguments)
                    accepted = check["edits"]
                    issue["action"] = "flagged"
                except Agent2Error:
                    pass
        issues.append(issue)
    value["edits"] = accepted
    validate_corrections(value, **arguments)
    return issues


def apply_corrections(units: list[dict[str, str]], edits: list[dict[str, Any]]) -> list[dict[str, str]]:
    result = []
    for unit in units:
        text = unit["text"]
        patches = []
        for edit in edits:
            if edit["unit_id"] == unit["unit_id"] and edit["decision"] == "apply":
                start, end = _span(text, edit["original"], edit["occurrence"])
                patches.append((start, end, edit["replacement"]))
        for start, end, replacement in sorted(patches, reverse=True):
            text = text[:start] + replacement + text[end:]
        result.append({"unit_id": unit["unit_id"], "text": text})
    return result


def provider_for_agent2(spec: ModelSpec) -> ChatProvider:
    if spec.provider == "codex":
        from codex_scaffold import CodexChatClient
        # Both correction and enrichment use the same subscription transport.
        # Bind the CLI model to the spec recorded in the run's cache identity.
        if not spec.model or spec.model == "default":
            raise Agent2Error("INVALID_CONFIG", "Agent2 Codex requires an explicit model")
        timeout = float(os.getenv("AGENT2_MODEL_TIMEOUT_SECONDS", "900"))
        if timeout <= 0:
            raise Agent2Error("INVALID_CONFIG", "AGENT2_MODEL_TIMEOUT_SECONDS must be positive")
        client = CodexChatClient(model=spec.model)
        client.config.timeout_seconds = timeout
        return client
    if spec.provider == "alibaba_cloud_model_studio":
        from qwen_chat_client import QwenChatClient
        from qwen_agent1 import load_qwen_config
        timeout = float(os.getenv("AGENT2_MODEL_TIMEOUT_SECONDS", "900"))
        if timeout <= 0:
            raise Agent2Error("INVALID_CONFIG", "AGENT2_MODEL_TIMEOUT_SECONDS must be positive")
        return QwenChatClient(replace(load_qwen_config(), timeout_seconds=timeout))
    if spec.provider == "deepseek":
        from deepseek_client import DeepSeekClient, load_deepseek_config
        return DeepSeekClient(load_deepseek_config())
    raise Agent2Error("UNSUPPORTED_PROVIDER", f"Unsupported Agent2 provider: {spec.provider}")


class Agent2ASRRun:
    def __init__(self, *, final_path: Path, lecture_root: Path, output_dir: Path,
                 course_id: str, lecture_id: str, session_id: str,
                 provider: ChatProvider, spec: ModelSpec, instructions: Any = (),
                 max_concurrency: int = 2) -> None:
        if max_concurrency < 1 or not spec.context_window_tokens or spec.context_window_tokens < 1:
            raise Agent2Error("INVALID_CONFIG", "Configure positive Agent2 concurrency and a verified model context window")
        self.final_path, self.output_dir = final_path, output_dir
        self.lecture_root = lecture_root
        self.course_id, self.lecture_id, self.session_id = course_id, lecture_id, session_id
        self.provider, self.spec, self.instructions = provider, spec, instructions
        self.max_concurrency = max_concurrency
        self.prompts = {stage: (PROMPT_ROOT / f"{stage}_v1.md").read_text(encoding="utf-8")
                        for stage in ("retrieval", "correction")}
        self.prompts["summary"] = SUMMARY_PROMPT.read_text(encoding="utf-8")
        self.prepared = False
        self.validation_issues: dict[str, list[dict[str, Any]]] = {}

    def prepare(self) -> None:
        source = load_final_source(self.final_path, course_id=self.course_id,
            lecture_id=self.lecture_id, session_id=self.session_id)
        agent1 = self.final_path.parent / "agent1"
        manifest = json.loads((agent1 / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("status") != "completed":
            raise Agent2Error("AGENT1_INCOMPLETE", "Complete Agent1 before starting ASR correction")
        self.units = read_jsonl(agent1 / "transcript_units.jsonl")
        if self.units != build_units(normalize_transcript(source)):
            raise Agent2Error("SOURCE_CHANGED", "Agent1 units do not match the final transcript")
        segmentation = json.loads((agent1 / "segmentation.json").read_text(encoding="utf-8"))
        errors = validate_segmentation(segmentation, self.units)
        if errors:
            raise Agent2Error("INVALID_AGENT1", "; ".join(errors))
        self.blocks = segmentation["blocks"]
        summaries = read_jsonl(agent1 / "blocks.jsonl")
        self.summaries = {summary["block_id"]: summary for summary in summaries}
        if len(self.summaries) != len(summaries) or set(self.summaries) != {block["block_id"] for block in self.blocks}:
            raise Agent2Error("INVALID_AGENT1", "Every segmented block needs one complete Agent1 summary")
        for block in self.blocks:
            if validate_summary(self.summaries[block["block_id"]], block):
                raise Agent2Error("INVALID_AGENT1", f"Invalid summary for {block['block_id']}")
        self.evidence = LectureEvidence(self.lecture_root)
        if self.evidence.scaffold["lecture_id"] != self.lecture_id:
            raise Agent2Error("INVALID_EVIDENCE", "Evidence belongs to a different lecture")
        self.fingerprint = _fingerprint({"source": source, "segmentation": segmentation,
            "summaries": summaries, "evidence": self.evidence.fingerprint,
            "prompts": self.prompts, "spec": self.spec.__dict__,
            "instructions": self.instructions, "policy": POLICY})
        self.run_dir = self.output_dir / "runs" / self.fingerprint
        self.prepared = True

    def _save_manifest(self) -> None:
        _write_json(self.run_dir / "manifest.json", self.manifest)
        _write_json(self.output_dir / "manifest.json", self.manifest)

    def _stage(self, block_id: str, stage: str) -> None:
        self.manifest["block_stages"][block_id] = stage
        self._save_manifest()

    def _error(self, block_id: str | None, stage: str, exc: Exception) -> None:
        record = {"block_id": block_id, "stage": stage,
                  "error_type": getattr(exc, "error_type", type(exc).__name__), "error_message": str(exc)}
        path = self.run_dir / "errors.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.manifest["root_cause"] = {key: record[key] for key in ("error_type", "error_message")}

    async def _call(self, stage: str, payload: Any) -> Any:
        prompt = self.prompts["correction" if stage == "correction_expanded" else stage]
        budget = len(prompt.encode()) + len(_json_bytes(payload)) + 1024 + (self.spec.max_output_tokens or 8192)
        if budget > self.spec.context_window_tokens:
            raise Agent2Error("CONTEXT_LIMIT_EXCEEDED",
                f"{stage} needs conservative budget {budget}; configured context limit is {self.spec.context_window_tokens}. No evidence was silently truncated.")
        kwargs = {"model": self.spec.model, "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": prompt},
                         {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]}
        if self.spec.max_output_tokens:
            kwargs["max_tokens"] = self.spec.max_output_tokens
        if self.spec.provider == "alibaba_cloud_model_studio":
            kwargs["stream"] = True
            kwargs["extra_body"] = {"enable_thinking": False}
        response = await self.provider.create_chat_completion(**kwargs)
        if hasattr(response, "__aiter__"):
            pieces = []
            async for chunk in response:
                choices = getattr(chunk, "choices", None)
                if choices:
                    choice = choices[0]
                    if getattr(choice, "finish_reason", None) == "length":
                        raise Agent2Error("TRUNCATED_RESPONSE", "Agent2 response reached its output limit")
                    content = getattr(getattr(choice, "delta", None), "content", None)
                    if isinstance(content, str):
                        pieces.append(content)
            return _parse_json("".join(pieces))
        choices = getattr(response, "choices", None)
        if choices and getattr(choices[0], "finish_reason", None) == "length":
            raise Agent2Error("TRUNCATED_RESPONSE", "Agent2 response reached its output limit")
        return _parse_json(_response_text(response))

    async def _validated_call(self, stage: str, payload: Any, validator: Callable[[Any], None],
                              block_id: str) -> Any:
        original = payload
        for repair in range(2):
            value = None
            model_response = None
            try:
                for attempt in range(4):
                    try:
                        value = await self._call(stage, payload)
                        break
                    except Exception as exc:
                        if not _retryable_provider_error(exc) or attempt == 3:
                            raise
                        self._error(block_id, stage + "_provider_retry", exc)
                        await asyncio.sleep(_retry_delay(attempt + 1))
                model_response = copy.deepcopy(value)
                validator(value)
                _write_json(self.run_dir / "responses" / block_id / f"{stage}-{uuid.uuid4().hex}.json",
                    {"input_fingerprint": _fingerprint(payload), "response": model_response, "resolved_response": value, "valid": True})
                return value
            except (ValueError, Agent1V1Error) as exc:
                _write_json(self.run_dir / "responses" / block_id / f"{stage}-{uuid.uuid4().hex}.json",
                    {"input_fingerprint": _fingerprint(payload), "response": model_response, "valid": False, "validation_error": str(exc)})
                if getattr(exc, "error_type", "") in {"CONTEXT_LIMIT_EXCEEDED", "TRUNCATED_RESPONSE"} or repair:
                    raise
                self._error(block_id, stage + "_validation", exc)
                payload = {"original_input": original, "original_output": model_response,
                           "validation_error": str(exc), "repair_instruction": "Repair the response using only original_input. Recheck EVERY unit_id, exact original span, occurrence and evidence reference, not just the first error. Return the required complete JSON."}
        raise AssertionError("repair loop ended unexpectedly")

    async def _cached_stage(self, block_id: str, stage: str, payload: Any,
                            validator: Callable[[Any], None]) -> Any:
        path = self.run_dir / "cache" / block_id / f"{stage}.json"
        digest = _fingerprint(payload)
        if path.is_file():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                if cached["input_fingerprint"] == digest:
                    validator(cached["result"])
                    if stage in {"correction", "correction_expanded"}:
                        issues = cached.get("validation_issues", []) + self.validation_issues.get(block_id, [])
                        self.validation_issues[block_id] = list({_fingerprint(issue): issue for issue in issues}.values())
                    return cached["result"]
            except (ValueError, KeyError, TypeError):
                pass
        self._stage(block_id, stage)
        value = await self._validated_call(stage, payload, validator, block_id)
        cached = {"input_fingerprint": digest, "result": value}
        if stage in {"correction", "correction_expanded"}:
            cached["validation_issues"] = self.validation_issues.get(block_id, [])
        _write_json(path, cached)
        return value

    async def _one_block(self, block: dict[str, str]) -> dict[str, Any]:
        block_id = block["block_id"]
        positions = {unit["unit_id"]: i for i, unit in enumerate(self.units)}
        start, end = positions[block["start_unit_id"]], positions[block["end_unit_id"]] + 1
        units = self.units[start:end]
        neighbors = self.units[max(0, start - 2):start] + self.units[end:end + 2]
        selection = await self._cached_stage(block_id, "retrieval", {
            "block_id": block_id, "title": block["title"], "agent1_summary": self.summaries[block_id],
            "transcript_units": units, "neighbor_units": neighbors,
            "indexes": self.evidence.index, "course_context": self.instructions},
            self.evidence.validate_selection)
        selected = self.evidence.selected_evidence(selection)
        correction_payload = {"block_id": block_id, "title": block["title"],
            "current_units": units, "neighbor_units": neighbors,
            "selected_evidence": selected,
            "evidence_provenance": EVIDENCE_PROVENANCE,
            "page_index": self.evidence.index["pages"], "course_context": self.instructions,
            "evidence_expansion_used": False}
        def check_correction(value: Any) -> None:
            self.validation_issues[block_id] = resolve_corrections(value, block_id=block_id, units=units, evidence=selected,
                page_ids=set(self.evidence.slides), expanded=correction_payload["evidence_expansion_used"])
        correction = await self._cached_stage(block_id, "correction", correction_payload, check_correction)
        extra = correction["additional_pages"]
        if extra:
            selected = self.evidence.selected_evidence(selection, extra)
            correction_payload = {**correction_payload, "selected_evidence": selected,
                                  "evidence_expansion_used": True}
            correction = await self._cached_stage(block_id, "correction_expanded", correction_payload, check_correction)
        corrected = apply_corrections(units, correction["edits"])
        def check_summary(value: Any) -> None:
            errors = validate_summary(value, block)
            if errors:
                raise Agent2Error("INVALID_SUMMARY", "; ".join(errors))
        summary = await self._cached_stage(block_id, "summary", {"schema_version": "1.0",
            "block_id": block_id, "title": block["title"],
            "transcript": " ".join(unit["text"] for unit in corrected),
            "transcript_units": corrected,
            "unresolved_corrections": [edit for edit in correction["edits"] if edit["decision"] == "flag"],
            "validation_issues": self.validation_issues.get(block_id, [])}, check_summary)
        self._stage(block_id, "completed")
        return {**block, "selection": selection, "evidence_pages": [p["page"] for p in selected["pages"]],
            "candidate_nodes": [{"node_id": node["node_id"], "title": node["title"]} for node in selected["nodes"]],
            "additional_pages": extra, "original_summary": self.summaries[block_id],
            "corrected_summary": summary, "original_units": units, "corrected_units": corrected,
            "edits": correction["edits"], "validation_issues": self.validation_issues.get(block_id, [])}

    def _publish(self, results: dict[str, dict[str, Any]]) -> None:
        ordered = [results[key] for key in sorted(results)]
        _write_jsonl(self.run_dir / "corrected_blocks.jsonl", ordered)
        _write_jsonl(self.run_dir / "retrieval.jsonl", [{"block_id": row["block_id"],
            **row["selection"], "evidence_pages": row["evidence_pages"], "additional_pages": row["additional_pages"]} for row in ordered])
        _write_jsonl(self.run_dir / "corrected_units.jsonl", [unit for row in ordered for unit in row["corrected_units"]])
        _write_jsonl(self.run_dir / "corrections.jsonl", [{"block_id": row["block_id"], **edit} for row in ordered for edit in row["edits"]])
        issues = [issue for row in ordered for issue in row.get("validation_issues", [])]
        _write_jsonl(self.run_dir / "validation_issues.jsonl", issues)
        self.manifest["completed_block_count"] = len(ordered)
        self.manifest["applied_edit_count"] = sum(edit["decision"] == "apply" for row in ordered for edit in row["edits"])
        self.manifest["flagged_edit_count"] = sum(edit["decision"] == "flag" for row in ordered for edit in row["edits"])
        self.manifest["validation_issue_count"] = len(issues)
        self.manifest["skipped_edit_count"] = sum(issue["action"] == "skipped" for issue in issues)
        self.manifest["review_item_count"] = self.manifest["flagged_edit_count"] + self.manifest["skipped_edit_count"]
        self._save_manifest()

    async def run(self) -> dict[str, Any]:
        if not self.prepared:
            self.prepare()
        previous = self.run_dir / "manifest.json"
        if previous.is_file():
            value = json.loads(previous.read_text(encoding="utf-8"))
            if value.get("status") == "completed":
                _write_json(self.output_dir / "manifest.json", value)
                return value
        self.manifest = {"schema_version": "1.0", "status": "processing", "input_fingerprint": self.fingerprint,
            "course_id": self.course_id, "lecture_id": self.lecture_id, "session_id": self.session_id,
            "model": self.spec.model, "provider": self.spec.provider, "policy": POLICY,
            "block_count": len(self.blocks), "completed_block_count": 0,
            "applied_edit_count": 0, "flagged_edit_count": 0, "failed_block_ids": [], "block_stages": {},
            "visual_policy": "text_only_flag_visual_uncertainty", "scaffold_fingerprint": self.evidence.fingerprint}
        _write_json(self.run_dir / "compact_index.json", self.evidence.index)
        self._save_manifest()
        results: dict[str, dict[str, Any]] = {}
        semaphore = asyncio.Semaphore(self.max_concurrency)
        async def worker(block: dict[str, str]) -> tuple[str, dict[str, Any] | None]:
            async with semaphore:
                try:
                    return block["block_id"], await self._one_block(block)
                except Exception as exc:
                    self._error(block["block_id"], self.manifest["block_stages"].get(block["block_id"], "block"), exc)
                    self._stage(block["block_id"], "failed")
                    return block["block_id"], None
        tasks = [asyncio.create_task(worker(block)) for block in self.blocks]
        try:
            for finished in asyncio.as_completed(tasks):
                block_id, result = await finished
                if result is None:
                    self.manifest["failed_block_ids"].append(block_id)
                else:
                    results[block_id] = result
                self._publish(results)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.manifest["failed_block_ids"].sort()
        self.manifest["status"] = "completed" if not self.manifest["failed_block_ids"] else "completed_with_errors" if results else "failed"
        if self.manifest["status"] == "completed":
            self.manifest.pop("root_cause", None)
        self._save_manifest()
        return self.manifest
