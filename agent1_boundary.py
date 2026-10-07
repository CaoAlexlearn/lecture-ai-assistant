"""Shared rolling semantic-boundary policy for production and replay."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from dotenv import load_dotenv
from openai import AsyncOpenAI

from agent1_models import Agent1BoundaryMetadata, SentenceId
from agent_context import build_agent1_boundary_context_blocks, model_messages
from token_buffer import BufferedTranscript, TokenCounter

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_BOUNDARY_PROMPT_PATH = PROJECT_ROOT / "agents" / "agent1_boundary.md"
DEFAULT_BOUNDARY_PROVIDER = "deepseek"
DEFAULT_BOUNDARY_MODEL = "deepseek-flash"
BOUNDARY_TYPES = frozenset(
    {
        "topic_transition",
        "completed_explanation",
        "completed_reasoning",
        "completed_example",
        "completed_question_answer",
        "completed_correction",
        "completed_list",
        "other",
    }
)


class Agent1BoundaryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Agent1BoundaryPolicy:
    check_start_tokens: int = 900
    min_block_tokens: int = 450
    lookahead_reserve_tokens: int = 180
    recheck_growth_tokens: int = 200
    max_tokens: int = 1_800

    def __post_init__(self) -> None:
        values = (
            self.check_start_tokens,
            self.min_block_tokens,
            self.recheck_growth_tokens,
            self.max_tokens,
        )
        if any(value <= 0 for value in values):
            raise ValueError("Boundary token thresholds must be positive")
        if self.lookahead_reserve_tokens < 0:
            raise ValueError("Boundary lookahead reserve cannot be negative")
        if self.min_block_tokens > self.check_start_tokens:
            raise ValueError("Boundary minimum block cannot exceed check start")
        if self.check_start_tokens > self.max_tokens:
            raise ValueError("Boundary check start cannot exceed hard max")
        if self.lookahead_reserve_tokens >= self.max_tokens:
            raise ValueError("Boundary lookahead reserve must be below hard max")


@dataclass(frozen=True, slots=True)
class Agent1BoundaryDetectorConfig:
    api_key: str = field(repr=False)
    base_url: str
    provider: str = DEFAULT_BOUNDARY_PROVIDER
    model: str = DEFAULT_BOUNDARY_MODEL
    prompt_path: Path = DEFAULT_BOUNDARY_PROMPT_PATH
    timeout_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class BoundarySentence:
    source_id: SentenceId
    source_text: str
    start_ms: int | None
    end_ms: int | None
    source_sequence: int | None


@dataclass(frozen=True, slots=True)
class BoundarySnapshot:
    sentences: tuple[BoundarySentence, ...]
    token_count: int
    source_sequence_start: int | None
    source_sequence_end: int | None

    @property
    def source_ids(self) -> tuple[SentenceId, ...]:
        return tuple(sentence.source_id for sentence in self.sentences)


@dataclass(frozen=True, slots=True)
class BoundaryDecision:
    decision: str
    cut_after_source_id: SentenceId | None
    boundary_type: str | None


@dataclass(frozen=True, slots=True)
class BoundaryResolution:
    cut_after_source_id: SentenceId
    metadata: Agent1BoundaryMetadata
    warning: str | None = None


class Agent1BoundaryDetector(Protocol):
    provider: str
    model: str

    async def detect(
        self, snapshot: BoundarySnapshot, *, hard_max_reached: bool
    ) -> BoundaryDecision: ...


class LastSentenceBoundaryDetector:
    """Deterministic compatibility detector for directly constructed test pipelines."""

    provider = "local"
    model = "last-sentence-boundary"

    async def detect(
        self, snapshot: BoundarySnapshot, *, hard_max_reached: bool
    ) -> BoundaryDecision:
        del hard_max_reached
        return BoundaryDecision("cut", snapshot.sentences[-1].source_id, "other")


class Agent1BoundaryController:
    """Own async-check eligibility while leaving transcript state in TokenBuffer."""

    STATE_SCHEMA_VERSION = 1

    def __init__(
        self,
        *,
        detector: Agent1BoundaryDetector,
        policy: Agent1BoundaryPolicy,
        token_counter: TokenCounter,
    ) -> None:
        self.detector = detector
        self.policy = policy
        self.token_counter = token_counter
        self.boundary_check_in_flight = False
        self.last_checked_token_count: int | None = None
        self.last_checked_source_sequence: int | None = None
        self.boundary_check_count = 0
        self._checks_since_emit = 0

    def snapshot(self, sources: Sequence[BufferedTranscript]) -> BoundarySnapshot:
        sentences = tuple(
            BoundarySentence(
                source_id=source.sentence_id,
                source_text=source.source_text,
                start_ms=source.start_ms,
                end_ms=source.end_ms,
                source_sequence=getattr(source, "source_sequence", None),
            )
            for source in sources
        )
        text = "\n".join(sentence.source_text for sentence in sentences)
        sequences = [
            sentence.source_sequence
            for sentence in sentences
            if sentence.source_sequence is not None
        ]
        return BoundarySnapshot(
            sentences=sentences,
            token_count=self.token_counter.count(text),
            source_sequence_start=sequences[0] if sequences else None,
            source_sequence_end=sequences[-1] if sequences else None,
        )

    def should_check(self, snapshot: BoundarySnapshot, *, force: bool = False) -> bool:
        if self.boundary_check_in_flight or not snapshot.sentences:
            return False
        if force:
            return snapshot.token_count >= self.policy.min_block_tokens
        if snapshot.token_count >= self.policy.max_tokens:
            return True
        if snapshot.token_count < self.policy.check_start_tokens:
            return False
        if self.last_checked_token_count is None:
            return True
        return (
            snapshot.token_count - self.last_checked_token_count
            >= self.policy.recheck_growth_tokens
        )

    def begin_check(self) -> int:
        if self.boundary_check_in_flight:
            raise RuntimeError("A boundary check is already in flight")
        self.boundary_check_in_flight = True
        self.boundary_check_count += 1
        self._checks_since_emit += 1
        return self._checks_since_emit

    def resolve(
        self,
        snapshot: BoundarySnapshot,
        live_sources: Sequence[BufferedTranscript],
        decision: BoundaryDecision | None,
        *,
        detector_error: str | None = None,
    ) -> BoundaryResolution | None:
        hard_max = snapshot.token_count >= self.policy.max_tokens
        invalid_reason = detector_error
        if invalid_reason is None:
            invalid_reason = self._validate_decision(snapshot, live_sources, decision)
        if (
            invalid_reason is None
            and decision is not None
            and decision.decision == "wait"
        ):
            if hard_max:
                invalid_reason = "Boundary detector returned WAIT at hard max"
            else:
                return None
        if invalid_reason is None and decision is not None:
            assert decision.cut_after_source_id is not None
            return self._resolution(
                snapshot,
                live_sources,
                decision.cut_after_source_id,
                boundary_type=decision.boundary_type,
                fallback=False,
            )
        if not hard_max:
            return None
        fallback_id = self._fallback_cut(snapshot)
        return self._resolution(
            snapshot,
            live_sources,
            fallback_id,
            boundary_type="other",
            fallback=True,
            warning=invalid_reason,
        )

    def finish_wait(self, snapshot: BoundarySnapshot) -> None:
        self.boundary_check_in_flight = False
        self.last_checked_token_count = snapshot.token_count
        self.last_checked_source_sequence = snapshot.source_sequence_end

    def finish_cut(self) -> None:
        self.boundary_check_in_flight = False
        self.last_checked_token_count = None
        self.last_checked_source_sequence = None
        self._checks_since_emit = 0

    def abort_check(self) -> None:
        self.boundary_check_in_flight = False

    def final_tail_metadata(self, snapshot: BoundarySnapshot) -> Agent1BoundaryMetadata:
        cut = snapshot.sentences[-1]
        return Agent1BoundaryMetadata(
            boundary_decision="final_tail",
            boundary_type="other",
            boundary_detector_provider=self.detector.provider,
            boundary_detector_model=self.detector.model,
            boundary_check_count=self._checks_since_emit,
            buffer_tokens_at_check=snapshot.token_count,
            prefix_tokens=snapshot.token_count,
            suffix_tokens=0,
            boundary_fallback=False,
            snapshot_source_sequence_start=snapshot.source_sequence_start,
            snapshot_source_sequence_end=snapshot.source_sequence_end,
            cut_source_sequence=cut.source_sequence,
        )

    def export_state(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.STATE_SCHEMA_VERSION,
            "lastCheckedTokenCount": self.last_checked_token_count,
            "lastCheckedSourceSequence": self.last_checked_source_sequence,
            "boundaryCheckCount": self.boundary_check_count,
            "checksSinceEmit": self._checks_since_emit,
        }

    def restore_state(self, state: dict[str, Any]) -> None:
        if state.get("schemaVersion") != self.STATE_SCHEMA_VERSION:
            raise ValueError("Unsupported boundary-controller checkpoint schema")
        last_tokens = state.get("lastCheckedTokenCount")
        last_sequence = state.get("lastCheckedSourceSequence")
        check_count = state.get("boundaryCheckCount")
        checks_since_emit = state.get("checksSinceEmit")
        if last_tokens is not None and (
            not isinstance(last_tokens, int) or last_tokens < 0
        ):
            raise ValueError("Invalid last boundary token count")
        if last_sequence is not None and (
            not isinstance(last_sequence, int) or last_sequence <= 0
        ):
            raise ValueError("Invalid last boundary source sequence")
        if not isinstance(check_count, int) or check_count < 0:
            raise ValueError("Invalid boundary check count")
        if not isinstance(checks_since_emit, int) or checks_since_emit < 0:
            raise ValueError("Invalid boundary checks-since-emit count")
        self.last_checked_token_count = last_tokens
        self.last_checked_source_sequence = last_sequence
        self.boundary_check_count = check_count
        self._checks_since_emit = checks_since_emit
        # In-flight HTTP work is intentionally not restored after a crash.
        self.boundary_check_in_flight = False

    def _validate_decision(
        self,
        snapshot: BoundarySnapshot,
        live_sources: Sequence[BufferedTranscript],
        decision: BoundaryDecision | None,
    ) -> str | None:
        if decision is None:
            return "Boundary detector returned no decision"
        if decision.decision == "wait":
            if (
                decision.cut_after_source_id is not None
                or decision.boundary_type is not None
            ):
                return "WAIT must not include a cut or boundary type"
            return None
        if decision.decision != "cut":
            return "Boundary decision must be CUT or WAIT"
        if decision.cut_after_source_id is None:
            return "CUT is missing cut_after_source_id"
        if decision.boundary_type not in BOUNDARY_TYPES:
            return "CUT has an unsupported boundary type"

        snapshot_ids = snapshot.source_ids
        try:
            cut_index = snapshot_ids.index(decision.cut_after_source_id)
        except ValueError:
            return "CUT source ID is not present in the immutable snapshot"
        if snapshot_ids.count(decision.cut_after_source_id) != 1:
            return "CUT source ID is not unique in the immutable snapshot"
        live_ids = tuple(source.sentence_id for source in live_sources)
        if (
            cut_index >= len(live_ids)
            or live_ids[: cut_index + 1] != snapshot_ids[: cut_index + 1]
        ):
            return "CUT snapshot prefix is stale relative to the live rolling buffer"

        prefix_tokens = self._count_snapshot_prefix(snapshot, cut_index + 1)
        if prefix_tokens < self.policy.min_block_tokens:
            return "CUT prefix is below the minimum block size"
        if snapshot.token_count < self.policy.max_tokens:
            latest_normal_cut = max(
                0, snapshot.token_count - self.policy.lookahead_reserve_tokens
            )
            if prefix_tokens > latest_normal_cut:
                return "CUT enters the protected lookahead reserve"
        return None

    def _resolution(
        self,
        snapshot: BoundarySnapshot,
        live_sources: Sequence[BufferedTranscript],
        cut_after_source_id: SentenceId,
        *,
        boundary_type: str | None,
        fallback: bool,
        warning: str | None = None,
    ) -> BoundaryResolution:
        live_ids = [source.sentence_id for source in live_sources]
        try:
            cut_index = live_ids.index(cut_after_source_id)
        except ValueError as exc:
            raise Agent1BoundaryError(
                "Resolved boundary disappeared from live buffer"
            ) from exc
        prefix_text = "\n".join(
            source.source_text for source in live_sources[: cut_index + 1]
        )
        suffix_text = "\n".join(
            source.source_text for source in live_sources[cut_index + 1 :]
        )
        cut_source = live_sources[cut_index]
        metadata = Agent1BoundaryMetadata(
            boundary_decision="cut",
            boundary_type=boundary_type,
            boundary_detector_provider=self.detector.provider,
            boundary_detector_model=self.detector.model,
            boundary_check_count=self._checks_since_emit,
            buffer_tokens_at_check=snapshot.token_count,
            prefix_tokens=self.token_counter.count(prefix_text),
            suffix_tokens=self.token_counter.count(suffix_text),
            boundary_fallback=fallback,
            snapshot_source_sequence_start=snapshot.source_sequence_start,
            snapshot_source_sequence_end=snapshot.source_sequence_end,
            cut_source_sequence=getattr(cut_source, "source_sequence", None),
        )
        return BoundaryResolution(cut_after_source_id, metadata, warning)

    def _fallback_cut(self, snapshot: BoundarySnapshot) -> SentenceId:
        target = max(1, snapshot.token_count - self.policy.lookahead_reserve_tokens)
        selected_index: int | None = None
        for index in range(1, len(snapshot.sentences) + 1):
            tokens = self._count_snapshot_prefix(snapshot, index)
            if tokens <= target:
                selected_index = index - 1
            else:
                break
        if selected_index is None:
            selected_index = 0
        return snapshot.sentences[selected_index].source_id

    def _count_snapshot_prefix(self, snapshot: BoundarySnapshot, count: int) -> int:
        return self.token_counter.count(
            "\n".join(sentence.source_text for sentence in snapshot.sentences[:count])
        )


class OpenAIAgent1BoundaryDetector:
    """Strict JSON-schema boundary detector using an OpenAI-compatible endpoint."""

    def __init__(
        self,
        config: Agent1BoundaryDetectorConfig,
        *,
        client: Any | None = None,
    ) -> None:
        self.config = config
        self.provider = config.provider
        self.model = config.model
        self.system_prompt = config.prompt_path.read_text(encoding="utf-8").strip()
        if not self.system_prompt:
            raise Agent1BoundaryError("Agent1 boundary prompt is empty")
        self.prompt_hash = hashlib.sha256(
            self.system_prompt.encode("utf-8")
        ).hexdigest()
        self.client = client or AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            max_retries=0,
        )

    async def detect(
        self, snapshot: BoundarySnapshot, *, hard_max_reached: bool
    ) -> BoundaryDecision:
        transcript = "\n".join(
            f"[{sentence.source_id}] {sentence.source_text}"
            for sentence in snapshot.sentences
        )
        blocks = build_agent1_boundary_context_blocks(
            system_prompt=self.system_prompt,
            transcript=transcript,
            hard_max_reached=hard_max_reached,
        )
        response = await self.client.chat.completions.create(
            model=self.config.model,
            messages=model_messages(blocks),
            temperature=0,
            response_format=self._response_format(),
        )
        choices = getattr(response, "choices", None)
        content = (
            getattr(getattr(choices[0], "message", None), "content", None)
            if choices
            else None
        )
        if not isinstance(content, str):
            raise Agent1BoundaryError("Boundary detector returned no JSON content")
        try:
            value = json.loads(content)
        except json.JSONDecodeError as exc:
            raise Agent1BoundaryError(
                "Boundary detector returned invalid JSON"
            ) from exc
        if not isinstance(value, dict) or set(value) != {
            "decision",
            "cut_after_source_id",
            "boundary_type",
        }:
            raise Agent1BoundaryError(
                "Boundary detector response does not match its schema"
            )
        decision = value["decision"]
        cut = value["cut_after_source_id"]
        boundary_type = value["boundary_type"]
        if decision not in {"cut", "wait"}:
            raise Agent1BoundaryError("Boundary detector decision is invalid")
        if cut is not None and not isinstance(cut, (str, int)):
            raise Agent1BoundaryError("Boundary detector source ID is invalid")
        if boundary_type is not None and boundary_type not in BOUNDARY_TYPES:
            raise Agent1BoundaryError("Boundary detector boundary type is invalid")
        return BoundaryDecision(decision, cut, boundary_type)

    def _response_format(self) -> dict[str, Any]:
        if self.provider == "deepseek":
            # DeepSeek's compatible endpoint supports JSON mode. Exact keys,
            # enum values, and nullable-field rules are enforced below.
            return {"type": "json_object"}
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "agent1_boundary_decision",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "decision": {"type": "string", "enum": ["cut", "wait"]},
                        "cut_after_source_id": {"type": ["string", "integer", "null"]},
                        "boundary_type": {
                            "type": ["string", "null"],
                            "enum": [*sorted(BOUNDARY_TYPES), None],
                        },
                    },
                    "required": [
                        "decision",
                        "cut_after_source_id",
                        "boundary_type",
                    ],
                },
            },
        }


def load_boundary_detector_config(
    *, api_key: str | None = None
) -> Agent1BoundaryDetectorConfig:
    load_dotenv()
    provider = (
        os.getenv("AGENT1_BOUNDARY_PROVIDER", DEFAULT_BOUNDARY_PROVIDER).strip()
        or DEFAULT_BOUNDARY_PROVIDER
    )
    configured_model = os.getenv("AGENT1_BOUNDARY_MODEL", "").strip()
    if provider == "deepseek":
        from deepseek_client import load_deepseek_config

        deepseek = load_deepseek_config()
        resolved_api_key = deepseek.api_key
        base_url = deepseek.base_url
        model = configured_model or deepseek.model
    else:
        resolved_api_key = api_key or os.getenv("DASHSCOPE_API_KEY", "").strip()
        base_url = (
            os.getenv("QWEN_BASE_URL", "").strip().rstrip("/")
            or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        )
        model = configured_model or "qwen3.7-flash"
    if not resolved_api_key:
        raise Agent1BoundaryError("Boundary detector API key is missing")
    try:
        timeout = float(os.getenv("AGENT1_BOUNDARY_TIMEOUT_SECONDS", "30"))
    except ValueError as exc:
        raise Agent1BoundaryError("Boundary detector timeout is invalid") from exc
    if timeout <= 0:
        raise Agent1BoundaryError("Boundary detector timeout must be positive")
    return Agent1BoundaryDetectorConfig(
        api_key=resolved_api_key,
        base_url=base_url,
        provider=provider,
        model=model,
        prompt_path=Path(
            os.getenv("AGENT1_BOUNDARY_PROMPT_PATH", str(DEFAULT_BOUNDARY_PROMPT_PATH))
        ),
        timeout_seconds=timeout,
    )
