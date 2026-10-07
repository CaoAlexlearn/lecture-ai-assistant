"""Incremental text translation through Alibaba Cloud Model Studio.

The backend uses the OpenAI-compatible Chat Completions endpoint and follows
WhisperLiveKit's five-method translation contract.  It translates the current
committed ASR text plus the unstable hypothesis tail for a responsive preview,
then emits an immutable Translation segment at punctuation/silence boundaries.

Unlike AlignAtt, a generic hosted chat model has no calibrated attention gate.
Therefore ``buffer_translation`` is a replaceable draft; only finalized
``Translation`` objects are append-only.
"""

from __future__ import annotations

from collections import deque
import json
import logging
import os
import re
import threading
from time import monotonic
from typing import Any, List, Optional, Tuple
from urllib import error as urllib_error
from urllib import request as urllib_request

from whisperlivekit.qwen_api_network import ensure_direct_connection
from whisperlivekit.timed_objects import ASRToken, HypothesisTail, TimedText, Translation
from whisperlivekit.translation_snapshot_stabilizer import (
    TranslationSnapshotStabilizer,
)

logger = logging.getLogger(__name__)

_CJK_LANGUAGE_PREFIXES = ("zh", "cmn", "yue", "ja")
_CLOSING_PUNCTUATION = set(",.!?;:%)]}\u3001\u3002\uff0c\uff01\uff1f\uff1b\uff1a\u3009\u300b\u300d\u300f\u3011\u3015\u3017\u3019\u301b")
_OPENING_PUNCTUATION = set("([{\u3008\u300a\u300c\u300e\u3010\u3014\u3016\u3018\u301a")


def reconstruct_source_text(parts: List[str], source_language: str = "auto") -> str:
    """Rebuild natural source text from tokens and a replaceable tail.

    Explicit whitespace carried by a backend is preserved. Otherwise English
    words receive spaces while punctuation and legacy character-token runs do
    not. This keeps the translation API independent from token timestamps.
    """

    values = [str(part) for part in parts if part is not None and str(part).strip()]
    if not values:
        return ""

    # Collapse legacy runs such as ["H", "e", "l", "l", "o"] before
    # applying word-level spacing. The dedicated snapshot path no longer emits
    # such tokens, but this prevents old sessions from producing "H e l l o".
    collapsed: list[str] = []
    index = 0
    while index < len(values):
        if len(values[index].strip()) == 1 and values[index].strip().isalnum():
            end = index
            while (
                end < len(values)
                and not values[end][:1].isspace()
                and len(values[end].strip()) == 1
                and values[end].strip().isalnum()
            ):
                end += 1
            if end - index >= 3:
                collapsed.append("".join(value.strip() for value in values[index:end]))
                index = end
                continue
        collapsed.append(values[index])
        index += 1

    compact_language = (source_language or "").lower().startswith(_CJK_LANGUAGE_PREFIXES)
    result = ""
    for value in collapsed:
        if not result:
            result = value.lstrip()
            continue
        if value[:1].isspace():
            result += value
            continue
        stripped = value.strip()
        if compact_language:
            result += stripped
        elif stripped[0] in _CLOSING_PUNCTUATION or stripped.startswith(("'", "\u2019")):
            result += stripped
        elif result[-1] in _OPENING_PUNCTUATION or result.endswith(("'", "\u2019", "-")):
            result += stripped
        else:
            result += " " + stripped
    return re.sub(r"[ \t]+", " ", result).strip()


def _compatible_base_url(explicit: Optional[str]) -> str:
    if explicit:
        return explicit.rstrip("/")
    configured = os.getenv("DASHSCOPE_COMPATIBLE_BASE_URL")
    if configured:
        return configured.rstrip("/")
    native = os.getenv("DASHSCOPE_BASE_URL")
    if native:
        native = native.rstrip("/")
        if native.endswith("/api/v1"):
            return native[:-len("/api/v1")] + "/compatible-mode/v1"
        return native
    return "https://dashscope.aliyuncs.com/compatible-mode/v1"


class QwenApiTranslationEngine:
    """Server-wide configuration that creates isolated translation sessions."""

    def __init__(
        self,
        model: str = "qwen3.7-flash",
        base_url: Optional[str] = None,
        timeout: float = 30.0,
        api_key: Optional[str] = None,
        source_language: str = "auto",
        debug_trace: bool = False,
        history_size: int = 3,
        agreement_rounds: int = 2,
        guard_units: int = 6,
        divergence_rounds: int = 2,
    ):
        self.model = model
        self.base_url = _compatible_base_url(base_url)
        ensure_direct_connection(self.base_url)
        self.timeout = float(timeout)
        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY")
        self.source_language = source_language
        self.debug_trace = debug_trace
        self.history_size = int(history_size)
        self.agreement_rounds = int(agreement_rounds)
        self.guard_units = int(guard_units)
        self.divergence_rounds = int(divergence_rounds)

    def new_stabilizer(self) -> TranslationSnapshotStabilizer:
        return TranslationSnapshotStabilizer(
            history_size=self.history_size,
            agreement_rounds=self.agreement_rounds,
            guard_units=self.guard_units,
            divergence_rounds=self.divergence_rounds,
            debug_trace=self.debug_trace,
        )

    def new_session(
        self,
        target_language: str,
        source_language: Optional[str] = None,
    ) -> "QwenApiTranslationClient":
        return QwenApiTranslationClient(
            engine=self,
            target_language=target_language,
            source_language=source_language or self.source_language,
        )


class QwenApiTranslationClient:
    """Per-stream Qwen translation state and API client."""

    wants_hypothesis_tail = True

    def __init__(
        self,
        engine: QwenApiTranslationEngine,
        target_language: str,
        source_language: str,
    ):
        self.engine = engine
        self.target_language = target_language
        self.source_language = source_language
        self.error = ""
        self._buffer_tokens: List[ASRToken] = []
        self._tail: Optional[HypothesisTail] = None
        self._pending_finals: List[Tuple[str, float, float, Optional[float]]] = []
        self._last_buffer = TimedText()
        self._cached_source = ""
        self._cached_translation = ""
        self._source_revision = 0
        self._last_source_signature: Optional[Tuple[str, str]] = None
        self._stabilizer = engine.new_stabilizer()
        self._closed = threading.Event()
        self.translation_latencies_ms = deque(maxlen=4096)
        self.end_to_end_latencies_ms = deque(maxlen=4096)

    @property
    def endpoint(self) -> str:
        base = self.engine.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def _join(self, parts: List[str]) -> str:
        return reconstruct_source_text(parts, self.source_language)

    def _committed_source(self) -> str:
        return self._join([token.text for token in self._buffer_tokens])

    def _draft_source(self) -> str:
        parts = [token.text for token in self._buffer_tokens]
        if self._tail and self._tail.text:
            parts.append(self._tail.text)
        return self._join(parts)

    def _revision_for(self, source_text: str, stable_source_text: str) -> int:
        signature = (source_text.strip(), stable_source_text.strip())
        if signature != self._last_source_signature:
            self._source_revision += 1
            self._last_source_signature = signature
        return self._source_revision

    def _reset_stabilization(self) -> None:
        self._stabilizer = self.engine.new_stabilizer()
        self._last_source_signature = None

    def insert_tokens(self, items: Any) -> None:
        if not isinstance(items, (list, tuple)):
            items = [items]
        for item in items:
            if isinstance(item, HypothesisTail):
                self._tail = item
                continue
            if not isinstance(item, ASRToken) or not item.text or not item.text.strip():
                continue
            # An old ASR tail may overlap newly committed tokens. A fresh tail,
            # when available, arrives after the commits in the same queue batch.
            self._tail = None
            self._buffer_tokens.append(item)
            if item.has_punctuation():
                text = self._committed_source()
                if text:
                    self._pending_finals.append(
                        (
                            text,
                            float(self._buffer_tokens[0].start or 0.0),
                            float(item.end or 0.0),
                            getattr(item, "source_event_received_at", None),
                        )
                    )
                self._buffer_tokens = []

    def translate(self, source_text: str) -> str:
        """Translate one already reconstructed source string."""

        source = source_text.strip()
        if self._closed.is_set():
            raise RuntimeError("Translation session is closed")
        if not self.engine.api_key:
            raise RuntimeError(
                "DASHSCOPE_API_KEY is not configured for Qwen translation."
            )
        if source == self._cached_source and self._cached_translation:
            return self._cached_translation

        system = (
            "You are a professional simultaneous interpreter. Translate the "
            f"user's text from {self.source_language or 'the detected language'} "
            f"to {self.target_language}. Return only the translation, with no "
            "explanation, labels, quotation marks, or added information. The "
            "input may be an incomplete live classroom-speech fragment. Be "
            "faithful and concise. When the input grows, preserve established "
            "wording whenever it remains correct; do not rewrite, polish, "
            "summarize, or restructure earlier text unnecessarily. Keep names, "
            "technical terms, numbers, and formulas consistent. Translate only "
            "the supplied text and never continue beyond it."
        )
        payload = {
            "model": self.engine.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": source},
            ],
            "temperature": 0.1,
            "max_completion_tokens": 512,
            "enable_thinking": False,
            "stream": False,
        }
        request = urllib_request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.engine.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        request_started = monotonic()
        try:
            with urllib_request.urlopen(request, timeout=self.engine.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib_error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(
                f"DashScope Qwen translation failed with HTTP {exc.code}: {detail}"
            ) from exc
        except urllib_error.URLError as exc:
            raise RuntimeError(
                f"DashScope Qwen translation request failed: {exc.reason}"
            ) from exc

        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            detail = body.get("message") if isinstance(body, dict) else None
            raise RuntimeError(
                "DashScope Qwen translation returned an unexpected response"
                + (f": {detail}" if detail else ".")
            ) from exc
        if isinstance(content, list):
            content = "".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict)
            )
        translation = str(content or "").strip()
        if not translation:
            raise RuntimeError("DashScope Qwen translation returned empty text.")
        translation_latency_ms = (monotonic() - request_started) * 1000.0
        self.translation_latencies_ms.append(translation_latency_ms)
        if self.engine.debug_trace:
            logger.info(
                "[QWEN TRANSLATION NETWORK] latency_ms=%.1f source_chars=%s",
                translation_latency_ms,
                len(source),
            )
        self._cached_source = source
        self._cached_translation = translation
        return translation

    def process(self) -> Tuple[Optional[Translation], TimedText]:
        completed: List[Translation] = []
        while self._pending_finals:
            source, start, end, source_event_received_at = self._pending_finals[0]
            try:
                translated = self.translate(source)
            except Exception as exc:
                self.error = f"Qwen translation incomplete: {exc}"
                logger.warning("%s", self.error)
                break
            self._pending_finals.pop(0)
            if source_event_received_at is not None:
                end_to_end_ms = (monotonic() - source_event_received_at) * 1000.0
                self.end_to_end_latencies_ms.append(end_to_end_ms)
                if self.engine.debug_trace:
                    logger.info(
                        "[QWEN END TO END] sentence_end_to_translation_ms=%.1f",
                        end_to_end_ms,
                    )
            revision = self._revision_for(source, source)
            self._stabilizer.observe(
                translated,
                revision,
                source,
                stable_source_text=source,
            )
            finalized = self._stabilizer.finalize(translated)
            if finalized.display_translation:
                completed.append(
                    Translation(
                        start=start,
                        end=end,
                        text=finalized.display_translation,
                    )
                )
            self._reset_stabilization()
            self._last_buffer = TimedText()
            self.error = ""

        # Do not mix a newer source segment into a prior segment whose final
        # translation is still pending after an API failure.
        if self._pending_finals:
            final = None
            if completed:
                final = Translation(
                    start=completed[0].start,
                    end=completed[-1].end,
                    text=" ".join(item.text for item in completed),
                )
            return final, self._last_buffer

        source = self._draft_source()
        if source:
            try:
                translated = self.translate(source)
                stable_source = self._committed_source()
                revision = self._revision_for(source, stable_source)
                stabilized = self._stabilizer.observe(
                    translated,
                    revision,
                    source,
                    stable_source_text=stable_source,
                )
                start = (
                    self._buffer_tokens[0].start
                    if self._buffer_tokens
                    else self._tail.start if self._tail else 0.0
                )
                end = (
                    self._tail.end
                    if self._tail and self._tail.end is not None
                    else self._buffer_tokens[-1].end if self._buffer_tokens else 0.0
                )
                self._last_buffer = TimedText(
                    start=start,
                    end=end,
                    text=stabilized.display_translation,
                )
                self.error = ""
            except Exception as exc:
                self.error = f"Qwen translation incomplete: {exc}"
                logger.warning("%s", self.error)
        else:
            self._last_buffer = TimedText()

        final = None
        if completed:
            final = Translation(
                start=completed[0].start,
                end=completed[-1].end,
                text=" ".join(item.text for item in completed),
            )
        return final, self._last_buffer

    def validate_buffer_and_reset(self) -> Tuple[Optional[Translation], TimedText]:
        source = self._draft_source()
        if source:
            starts = [token.start for token in self._buffer_tokens if token.start is not None]
            if self._tail and self._tail.start is not None:
                starts.append(self._tail.start)
            ends = [token.end for token in self._buffer_tokens if token.end is not None]
            if self._tail and self._tail.end is not None:
                ends.append(self._tail.end)
            self._pending_finals.append(
                (
                    source,
                    float(min(starts) if starts else 0.0),
                    float(max(ends) if ends else 0.0),
                    None,
                )
            )
        self._buffer_tokens = []
        self._tail = None
        return self.process()

    def finish(self) -> Tuple[Optional[Translation], TimedText]:
        return self.validate_buffer_and_reset()

    def insert_silence(self, duration: float = None) -> None:
        pass

    def close(self) -> None:
        self._closed.set()
        if self.translation_latencies_ms or self.end_to_end_latencies_ms:
            logger.info("QWEN_TRANSLATION_METRICS %s", self.metrics())

    @staticmethod
    def _latency_summary(values) -> dict[str, float | int]:
        items = sorted(values)
        if not items:
            return {"count": 0, "avg": 0.0, "p95": 0.0}
        index = min(len(items) - 1, int(len(items) * 0.95))
        return {
            "count": len(items),
            "avg": round(sum(items) / len(items), 2),
            "p95": round(items[index], 2),
        }

    def metrics(self) -> dict[str, dict[str, float | int]]:
        return {
            "translation_latency_ms": self._latency_summary(
                self.translation_latencies_ms
            ),
            "sentence_end_to_translation_ms": self._latency_summary(
                self.end_to_end_latencies_ms
            ),
        }
