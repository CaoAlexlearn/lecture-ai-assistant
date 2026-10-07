"""Post-class Agent1: one whole-lecture segmenter, then bounded block workers.

This module deliberately has no dependency on the live Agent1 queue or PreAgent.
No provider is created until an explicit run is requested.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

import httpx
import openai


SCHEMA_VERSION = "1.0"
PROMPT_VERSION = "v1"
REQUEST_POLICY = "provider_default_thinking_stream_v1"
PROVIDER_MAX_ATTEMPTS = 4
PROMPT_ROOT = Path(__file__).resolve().parent / "agent1_v1_prompts"


class Agent1V1Error(RuntimeError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


class ChatProvider(Protocol):
    async def create_chat_completion(self, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model: str
    context_window_tokens: int | None = None
    max_output_tokens: int | None = None


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json(path: Path, value: Any) -> None:
    _atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    _atomic_write(path, "".join(json.dumps(v, ensure_ascii=False) + "\n" for v in values))


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_final_source(path: Path, *, course_id: str, lecture_id: str, session_id: str) -> str:
    try:
        value = _read_json(path)
    except (OSError, ValueError) as exc:
        raise Agent1V1Error("INVALID_FINAL", f"Cannot read final transcript: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1 or value.get("status") != "final":
        raise Agent1V1Error("INVALID_FINAL", "Expected schema_version=1 and status=final")
    for key, expected in (("course_id", course_id), ("lecture_id", lecture_id), ("session_id", session_id)):
        if value.get(key) != expected:
            raise Agent1V1Error("INVALID_FINAL", f"{key} does not match requested session")
    source = value.get("source")
    committed = source.get("committed") if isinstance(source, dict) else None
    if not isinstance(committed, str) or not committed.strip():
        raise Agent1V1Error("INVALID_FINAL", "source.committed is empty or invalid")
    return committed


def normalize_transcript(text: str) -> str:
    """Remove only adjacent exact duplicates; retain every uncertain repetition."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    deduped: list[str] = []
    for line in lines:
        if not deduped or line != deduped[-1]:
            deduped.append(line)
    value = " ".join(" ".join(deduped).split())
    # Exact adjacent multi-sentence ASR replay is detectable without semantic
    # guessing. Require substantial length, so a teacher's short emphasis stays.
    sentences = _sentence_parts(value)
    output: list[str] = []
    index = 0
    while index < len(sentences):
        removed = False
        for width in range(min(8, (len(sentences) - index) // 2), 0, -1):
            first = sentences[index:index + width]
            second = sentences[index + width:index + 2 * width]
            phrase = " ".join(first)
            if first == second and len(phrase) >= 100 and len(phrase.split()) >= 15:
                output.extend(first)
                index += 2 * width
                removed = True
                break
        if not removed:
            output.append(sentences[index])
            index += 1
    return " ".join(output)


_ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "prof", "e.g", "i.e", "etc", "vs", "fig", "no", "st"}


def _sentence_parts(text: str) -> list[str]:
    value = " ".join(text.split())
    if not value:
        return []
    parts: list[str] = []
    start = 0
    for match in re.finditer(r"[.!?]+(?=\s|$)", value):
        stop = match.end()
        if value[match.start()] == ".":
            preceding = value[start:match.start()].split()
            last = preceding[-1].lower().rstrip(".") if preceding else ""
            if last in _ABBREVIATIONS or re.fullmatch(r"[A-Za-z]", last):
                continue
            if match.start() and match.start() + 1 < len(value) and value[match.start() - 1].isdigit() and value[match.start() + 1].isdigit():
                continue
        part = value[start:stop].strip()
        if part:
            parts.append(part)
        start = stop
    tail = value[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def build_units(normalized_text: str) -> list[dict[str, str]]:
    parts = _sentence_parts(normalized_text)
    merged: list[str] = []
    for part in parts:
        if merged and len(part.split()) <= 2:
            merged[-1] += " " + part
        else:
            merged.append(part)
    if len(merged) > 1 and len(merged[0].split()) <= 2:
        merged[1] = merged[0] + " " + merged[1]
        merged.pop(0)
    # Long unpunctuated ASR stretches are divided only at existing clause
    # punctuation. No text is invented, reordered, or deleted.
    units: list[str] = []
    for part in merged:
        if len(part.split()) <= 70:
            units.append(part)
            continue
        clauses = re.split(r"(?<=[,;:])\s+", part)
        current = ""
        for clause in clauses:
            if current and len((current + " " + clause).split()) > 70 and len(current.split()) >= 25:
                units.append(current)
                current = clause
            else:
                current = (current + " " + clause).strip()
        if current:
            units.append(current)
    return [{"unit_id": f"U{i:04d}", "text": part} for i, part in enumerate(units, 1)]


def validate_segmentation(value: Any, units: list[dict[str, str]]) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict) or set(value) != {"schema_version", "blocks"} or value.get("schema_version") != SCHEMA_VERSION:
        return ["segmentation must contain only schema_version=1.0 and blocks"]
    blocks = value["blocks"]
    if not isinstance(blocks, list) or not blocks:
        return ["blocks must be a non-empty array"]
    id_to_index = {unit["unit_id"]: index for index, unit in enumerate(units)}
    next_index = 0
    for number, block in enumerate(blocks, 1):
        if not isinstance(block, dict) or set(block) != {"block_id", "start_unit_id", "end_unit_id", "title"}:
            errors.append(f"block {number}: invalid fields")
            continue
        if block["block_id"] != f"B{number:03d}":
            errors.append(f"block {number}: block_id must be B{number:03d}")
        if not isinstance(block["title"], str) or not block["title"].strip():
            errors.append(f"block {number}: title is empty")
        start = id_to_index.get(block["start_unit_id"]) if isinstance(block["start_unit_id"], str) else None
        end = id_to_index.get(block["end_unit_id"]) if isinstance(block["end_unit_id"], str) else None
        if start is None or end is None:
            errors.append(f"block {number}: nonexistent unit_id")
            continue
        if start > end:
            errors.append(f"block {number}: reversed or empty range")
        if start > next_index:
            errors.append(f"block {number}: gap before {block['start_unit_id']}")
        if start < next_index:
            errors.append(f"block {number}: overlap or out of order at {block['start_unit_id']}")
        next_index = max(next_index, end + 1)
    if not isinstance(blocks[0], dict) or blocks[0].get("start_unit_id") != units[0]["unit_id"]:
        errors.append("first block must start at U0001")
    if not isinstance(blocks[-1], dict) or blocks[-1].get("end_unit_id") != units[-1]["unit_id"]:
        errors.append("last block must cover final unit")
    if next_index != len(units):
        errors.append("not every unit is covered exactly once")
    return errors


def reconstruct_block(block: dict[str, str], units: list[dict[str, str]]) -> str:
    positions = {unit["unit_id"]: index for index, unit in enumerate(units)}
    return " ".join(unit["text"] for unit in units[positions[block["start_unit_id"]]:positions[block["end_unit_id"]] + 1])


_SUMMARY_FIELDS = {"block_id", "title", "summary", "how_teacher_explained", "examples", "emphasis"}


def validate_summary(value: Any, block: dict[str, str]) -> list[str]:
    if not isinstance(value, dict) or set(value) != _SUMMARY_FIELDS:
        return ["summary has missing or unknown fields"]
    errors: list[str] = []
    for key in ("block_id", "title"):
        if value[key] != block[key]:
            errors.append(f"{key} differs from input")
    for key in ("summary", "how_teacher_explained", "examples", "emphasis"):
        items = value[key]
        if not isinstance(items, list) or any(not isinstance(item, str) or not item.strip() for item in items):
            errors.append(f"{key} must be an array of non-empty strings")
    if isinstance(value["summary"], list) and not value["summary"]:
        errors.append("summary must be non-empty")
    return errors


def _response_text(response: Any) -> str:
    if isinstance(response, str):
        return response
    choices = getattr(response, "choices", None)
    content = getattr(getattr(choices[0], "message", None), "content", None) if choices else None
    if not isinstance(content, str):
        raise Agent1V1Error("EMPTY_RESPONSE", "Model returned no text")
    return content


def _parse_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Agent1V1Error("INVALID_JSON", str(exc)) from exc


def _retryable_provider_error(exc: Exception) -> bool:
    if isinstance(exc, openai.APIStatusError):
        return exc.status_code in (408, 409, 429) or exc.status_code >= 500
    return isinstance(exc, (openai.APIConnectionError, httpx.TransportError,
                            ConnectionError, TimeoutError))


def _retry_delay(request_attempt: int) -> float:
    return 2 ** (request_attempt - 1) + random.uniform(0, 0.5)


class Agent1V1Run:
    def __init__(self, *, final_path: Path, output_dir: Path, course_id: str, lecture_id: str,
                 session_id: str, segmenter: ChatProvider, summarizer: ChatProvider,
                 segmenter_spec: ModelSpec, summarizer_spec: ModelSpec,
                 lecture_context: str | None = None, max_concurrency: int = 4,
                 runtime_dir: Path | None = None, allow_new_run: bool = False) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self.final_path, self.output_dir = final_path, output_dir
        self.course_id, self.lecture_id, self.session_id = course_id, lecture_id, session_id
        self.segmenter, self.summarizer = segmenter, summarizer
        self.segmenter_spec, self.summarizer_spec = segmenter_spec, summarizer_spec
        self.lecture_context = lecture_context
        self.max_concurrency = max_concurrency
        self.runtime_dir = runtime_dir or output_dir.parent / "runtime" / "agent1"
        self.allow_new_run = allow_new_run
        self.segmenter_prompt = (PROMPT_ROOT / "segmenter_v1.md").read_text(encoding="utf-8").strip()
        self.summarizer_prompt = (PROMPT_ROOT / "summarizer_v1.md").read_text(encoding="utf-8").strip()
        self.errors: list[dict[str, Any]] = []

    def _error(self, stage: str, block_id: str | None, attempt: int, exc: Exception) -> None:
        record = {"stage": stage, "block_id": block_id, "attempt": attempt,
                  "error_type": getattr(exc, "error_type", type(exc).__name__),
                  "error_message": str(exc)}
        self.errors.append(record)
        path = self.runtime_dir / "errors.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    async def _call(self, client: ChatProvider, spec: ModelSpec, prompt: str, payload: Any) -> Any:
        request: dict[str, Any] = {"model": spec.model, "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ], "temperature": 0}
        if spec.max_output_tokens is not None:
            request["max_tokens"] = spec.max_output_tokens
        if spec.provider == "alibaba_cloud_model_studio":
            request["stream"] = True
        response = await client.create_chat_completion(**request)
        if hasattr(response, "__aiter__"):
            pieces: list[str] = []
            finish_reason: str | None = None
            async for chunk in response:
                choices = getattr(chunk, "choices", None)
                if not choices:
                    continue
                choice = choices[0]
                delta = getattr(choice, "delta", None)
                content = getattr(delta, "content", None)
                if isinstance(content, str):
                    pieces.append(content)
                if getattr(choice, "finish_reason", None):
                    finish_reason = choice.finish_reason
            if finish_reason == "length":
                raise Agent1V1Error("TRUNCATED_RESPONSE", "Model stopped at its output token limit")
            if not pieces:
                raise Agent1V1Error("EMPTY_RESPONSE", "Model stream contained no answer content")
            return _parse_json("".join(pieces))
        return _parse_json(_response_text(response))

    async def _call_with_retry(self, client: ChatProvider, spec: ModelSpec, prompt: str,
                               payload: Any, *, stage: str, block_id: str | None) -> Any:
        for request_attempt in range(1, PROVIDER_MAX_ATTEMPTS + 1):
            try:
                return await self._call(client, spec, prompt, payload)
            except Exception as exc:
                if not _retryable_provider_error(exc) or request_attempt == PROVIDER_MAX_ATTEMPTS:
                    raise
                self._error(f"{stage}_provider_retry", block_id, request_attempt, exc)
                await asyncio.sleep(_retry_delay(request_attempt))
        raise AssertionError("provider retry loop ended unexpectedly")

    def _check_context(self, payload: Any) -> None:
        limit = self.segmenter_spec.context_window_tokens
        if limit is None or limit <= 0:
            raise Agent1V1Error("CONTEXT_LIMIT_UNKNOWN", "Configure the segmenter model's verified context window")
        # UTF-8 bytes plus fixed message overhead is a conservative upper bound
        # for ordinary English and avoids a provider-specific tokenizer contract.
        estimate = len(self.segmenter_prompt.encode("utf-8")) + len(_json_bytes(payload)) + 1024
        reserve = self.segmenter_spec.max_output_tokens or 4096
        if estimate + reserve > limit:
            raise Agent1V1Error("CONTEXT_LIMIT_EXCEEDED", f"Whole lecture needs conservative budget {estimate + reserve}, limit is {limit}; hierarchical fallback is not implemented")

    async def _segmentation(self, units: list[dict[str, str]], run_fingerprint: str) -> dict[str, Any]:
        manifest_path = self.output_dir / "manifest.json"
        segmentation_path = self.output_dir / "segmentation.json"
        if manifest_path.exists() and segmentation_path.exists():
            prior = _read_json(manifest_path)
            if prior.get("input_fingerprint") != run_fingerprint:
                raise Agent1V1Error("INPUT_CHANGED", "Existing Agent1 artifacts have a different source, context, model, or prompt fingerprint")
            candidate = _read_json(segmentation_path)
            errors = validate_segmentation(candidate, units)
            if not errors:
                return candidate
        payload = {"schema_version": SCHEMA_VERSION, "lecture": {"course_id": self.course_id,
                   "lecture_id": self.lecture_id}, "units": units}
        if self.lecture_context:
            payload["lecture_context"] = self.lecture_context
        candidate: Any = None
        for attempt in (1, 2):
            request = payload if attempt == 1 else {"original_input": payload,
                "original_segmentation": candidate, "validator_errors": errors,
                "repair_instruction": "Change only block boundaries and titles. Return the required segmentation schema."}
            try:
                self._check_context(request)
                candidate = await self._call_with_retry(self.segmenter, self.segmenter_spec,
                    self.segmenter_prompt, request, stage="segmenter", block_id=None)
                errors = validate_segmentation(candidate, units)
            except Agent1V1Error as exc:
                self._error("segmenter", None, attempt, exc)
                if exc.error_type != "INVALID_JSON" or attempt == 2:
                    raise
                errors = [str(exc)]
                continue
            except Exception as exc:
                self._error("segmenter", None, attempt, exc)
                raise
            if not errors:
                _write_json(segmentation_path, candidate)
                return candidate
            self._error("segmenter_validation", None, attempt, Agent1V1Error("INVALID_SEGMENTATION", "; ".join(errors)))
        raise Agent1V1Error("INVALID_SEGMENTATION", "; ".join(errors))

    async def _one_block(self, block: dict[str, str], transcript: str, fingerprint: str) -> tuple[str, dict[str, Any] | None]:
        block_id = block["block_id"]
        cache = self.runtime_dir / "blocks" / f"{fingerprint}.json"
        legacy_cache = self.runtime_dir / "blocks" / f"{block_id}.json"
        if not cache.exists() and legacy_cache.exists():
            try:
                legacy = _read_json(legacy_cache)
                if legacy.get("input_fingerprint") == fingerprint:
                    _write_json(cache, legacy)
            except (OSError, ValueError):
                pass
        if cache.exists():
            try:
                previous = _read_json(cache)
                if previous.get("input_fingerprint") == fingerprint and not validate_summary(previous.get("result"), block):
                    return block_id, previous["result"]
            except (OSError, ValueError):
                pass
        payload = {"schema_version": SCHEMA_VERSION, "block_id": block_id,
                   "title": block["title"], "transcript": transcript}
        prior: Any = None
        for attempt in (1, 2):
            request = payload if attempt == 1 else {"original_input": payload,
                "original_output": prior, "validator_errors": errors,
                "repair_instruction": "Repair only the structured output. Keep block_id and title unchanged; use transcript evidence only."}
            try:
                result = await self._call_with_retry(self.summarizer, self.summarizer_spec,
                    self.summarizer_prompt, request, stage="summarizer", block_id=block_id)
                errors = validate_summary(result, block)
            except Agent1V1Error as exc:
                self._error("summarizer", block_id, attempt, exc)
                if exc.error_type != "INVALID_JSON" or attempt == 2:
                    return block_id, None
                errors = [str(exc)]
                prior = None
                continue
            except Exception as exc:
                self._error("summarizer", block_id, attempt, exc)
                return block_id, None
            if not errors:
                _write_json(cache, {"input_fingerprint": fingerprint, "result": result})
                return block_id, result
            prior = result
            self._error("summarizer_validation", block_id, attempt, Agent1V1Error("INVALID_SUMMARY", "; ".join(errors)))
        return block_id, None

    async def run(self) -> dict[str, Any]:
        try:
            source = load_final_source(self.final_path, course_id=self.course_id,
                                       lecture_id=self.lecture_id, session_id=self.session_id)
        except Exception as exc:
            self._error("source", None, 0, exc)
            manifest = {"schema_version": SCHEMA_VERSION, "status": "failed",
                "root_cause": {"error_type": getattr(exc, "error_type", type(exc).__name__),
                               "error_message": str(exc)}}
            if not (self.output_dir / "manifest.json").exists():
                _write_json(self.output_dir / "manifest.json", manifest)
            return manifest
        normalized = normalize_transcript(source)
        units = build_units(normalized)
        if not units:
            raise Agent1V1Error("EMPTY_TRANSCRIPT", "No transcript units were produced")
        run_fingerprint = _fingerprint({"source": source, "context": self.lecture_context,
            "segmenter": self.segmenter_spec.__dict__, "segmenter_prompt": self.segmenter_prompt,
            "summarizer": self.summarizer_spec.__dict__, "summarizer_prompt": self.summarizer_prompt,
            "request_policy": REQUEST_POLICY})
        manifest_path = self.output_dir / "manifest.json"
        if manifest_path.exists():
            previous = _read_json(manifest_path)
            if previous.get("input_fingerprint") not in (None, run_fingerprint):
                if not self.allow_new_run:
                    exc = Agent1V1Error("INPUT_CHANGED", "Existing Agent1 run has a different fingerprint; prior completed artifacts were preserved")
                    self._error("run", None, 0, exc)
                    return {"schema_version": SCHEMA_VERSION, "status": "failed",
                        "root_cause": {"error_type": exc.error_type, "error_message": str(exc)}}
                old_fingerprint = previous["input_fingerprint"]
                if not isinstance(old_fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", old_fingerprint):
                    raise Agent1V1Error("INVALID_CACHE", "Invalid prior Agent1 run identity")
                import shutil
                names = ("manifest.json", "segmentation.json", "transcript_units.jsonl", "blocks.jsonl")
                archive = self.output_dir / "runs" / old_fingerprint
                archive.mkdir(parents=True, exist_ok=True)
                for name in names:
                    path = self.output_dir / name
                    if path.exists():
                        shutil.copy2(path, archive / name)
                cached_run = self.output_dir / "runs" / run_fingerprint
                for name in names:
                    path = self.output_dir / name
                    if (cached_run / name).exists():
                        shutil.copy2(cached_run / name, path)
                    elif path.exists():
                        path.unlink()
        _write_jsonl(self.output_dir / "transcript_units.jsonl", units)
        manifest: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "status": "processing",
            "input_fingerprint": run_fingerprint, "segmenter_model": self.segmenter_spec.model,
            "summarizer_model": self.summarizer_spec.model,
            "segmenter_provider": self.segmenter_spec.provider,
            "summarizer_provider": self.summarizer_spec.provider,
            "segmenter_prompt_version": PROMPT_VERSION, "summarizer_prompt_version": PROMPT_VERSION,
            "thinking_policy": "provider_default", "request_policy": REQUEST_POLICY,
            "unit_count": len(units), "block_count": 0, "completed_block_count": 0, "failed_block_ids": []}
        _write_json(manifest_path, manifest)
        try:
            segmentation = await self._segmentation(units, run_fingerprint)
            blocks = segmentation["blocks"]
            manifest["block_count"] = len(blocks)
            _write_json(manifest_path, manifest)
            semaphore = asyncio.Semaphore(self.max_concurrency)
            results: dict[str, dict[str, Any]] = {}
            async def worker(block: dict[str, str]) -> tuple[str, dict[str, Any] | None]:
                transcript = reconstruct_block(block, units)
                fingerprint = _fingerprint({"block": block, "transcript": transcript,
                    "model": self.summarizer_spec.__dict__, "prompt": self.summarizer_prompt,
                    "request_policy": REQUEST_POLICY})
                async with semaphore:
                    return await self._one_block(block, transcript, fingerprint)
            tasks = [asyncio.create_task(worker(block)) for block in blocks]
            for finished in asyncio.as_completed(tasks):
                block_id, result = await finished
                if result is not None:
                    results[block_id] = result
                else:
                    manifest["failed_block_ids"].append(block_id)
                _write_jsonl(self.output_dir / "blocks.jsonl", [results[key] for key in sorted(results)])
                manifest["completed_block_count"] = len(results)
                _write_json(manifest_path, manifest)
            manifest["failed_block_ids"].sort()
            manifest["status"] = ("failed" if not results else
                "completed_with_errors" if manifest["failed_block_ids"] else "completed")
            if not results and self.errors:
                last = self.errors[-1]
                manifest["root_cause"] = {"error_type": last["error_type"],
                                          "error_message": last["error_message"]}
        except Exception as exc:
            self._error("run", None, 0, exc)
            manifest["status"] = "failed"
            manifest["root_cause"] = {"error_type": getattr(exc, "error_type", type(exc).__name__),
                                      "error_message": str(exc)}
        _write_json(manifest_path, manifest)
        return manifest


def provider_from_spec(spec: ModelSpec) -> ChatProvider:
    """Reuse the project's OpenAI-compatible Qwen/DeepSeek chat adapters."""
    if spec.provider == "codex":
        from codex_scaffold import CodexChatClient
        if not spec.model or spec.model == "default":
            raise Agent1V1Error("INVALID_CONFIG", "Agent1 Codex requires an explicit model")
        timeout = float(os.getenv("AGENT1_MODEL_TIMEOUT_SECONDS", "900"))
        if timeout <= 0:
            raise Agent1V1Error("INVALID_CONFIG", "AGENT1_MODEL_TIMEOUT_SECONDS must be positive")
        client = CodexChatClient(model=spec.model)
        client.config.timeout_seconds = timeout
        return client
    if spec.provider == "alibaba_cloud_model_studio":
        from qwen_chat_client import QwenChatClient
        from qwen_agent1 import load_qwen_config
        try:
            timeout = float(os.getenv("AGENT1_MODEL_TIMEOUT_SECONDS", "900"))
        except ValueError as exc:
            raise Agent1V1Error("INVALID_CONFIG", "AGENT1_MODEL_TIMEOUT_SECONDS must be a number") from exc
        if timeout <= 0:
            raise Agent1V1Error("INVALID_CONFIG", "AGENT1_MODEL_TIMEOUT_SECONDS must be positive")
        return QwenChatClient(replace(load_qwen_config(), timeout_seconds=timeout))
    if spec.provider == "deepseek":
        from deepseek_client import DeepSeekClient, load_deepseek_config
        return DeepSeekClient(load_deepseek_config())
    raise Agent1V1Error("UNSUPPORTED_PROVIDER", f"Unsupported provider: {spec.provider}")
