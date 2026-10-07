"""Conservative Local Agreement for revisable translation snapshots.

Remote LLM translation APIs return a complete translation on every request.
The wording can change even when all variants are semantically acceptable, so
this stabilizer exposes an append-only target prefix plus a replaceable tail.
It deliberately uses deterministic text agreement rather than semantic
similarity or unavailable attention weights.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
import logging
import re
import unicodedata
from typing import Deque, Iterable, Literal, Optional, Sequence


logger = logging.getLogger(__name__)

_UNIT_RE = re.compile(
    r"[A-Za-z0-9]+(?:[._+\-/][A-Za-z0-9]+)*%?"
    r"|[\u3400-\u4dbf\u4e00-\u9fff]"
    r"|[^\s]",
    re.UNICODE,
)


@dataclass(frozen=True)
class TranslationUnit:
    raw: str
    normalized: str
    kind: Literal["lexical", "cjk", "punctuation"]
    prefix: str = ""

    @property
    def surface(self) -> str:
        return self.prefix + self.raw


@dataclass(frozen=True)
class TranslationStabilizationUpdate:
    committed_delta: str
    committed_translation: str
    unstable_translation: str
    display_translation: str
    accepted: bool
    status: str
    source_revision: object = None
    alignment_mode: str = ""
    matched_anchor: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Alignment:
    mode: str
    candidate: tuple[TranslationUnit, ...]
    anchor: tuple[str, ...] = ()
    anchored: bool = True


def tokenize_translation(text: str) -> list[TranslationUnit]:
    """Tokenize CJK by character while retaining ASCII lexical units."""

    raw_text = (text or "").strip()
    units: list[TranslationUnit] = []
    previous_end = 0
    for match in _UNIT_RE.finditer(raw_text):
        raw = match.group(0)
        category = unicodedata.category(raw[0])
        if len(raw) == 1 and "\u3400" <= raw <= "\u9fff":
            kind: Literal["lexical", "cjk", "punctuation"] = "cjk"
        elif category.startswith(("P", "S")) and not raw.endswith("%"):
            kind = "punctuation"
        else:
            kind = "lexical"
        normalized = unicodedata.normalize("NFKC", raw).casefold()
        units.append(
            TranslationUnit(
                raw=raw,
                normalized=normalized,
                kind=kind,
                prefix=raw_text[previous_end:match.start()],
            )
        )
        previous_end = match.end()
    return units


def render_translation(units: Iterable[TranslationUnit]) -> str:
    return "".join(unit.surface for unit in units)


def _positions(units: Sequence[TranslationUnit]) -> list[int]:
    return [index for index, unit in enumerate(units) if unit.kind != "punctuation"]


def _keys(units: Sequence[TranslationUnit]) -> list[str]:
    return [unit.normalized for unit in units if unit.kind != "punctuation"]


def _prefix_length(
    left: Sequence[TranslationUnit],
    right: Sequence[TranslationUnit],
) -> int:
    left_keys = _keys(left)
    right_keys = _keys(right)
    for index in range(min(len(left_keys), len(right_keys))):
        if left_keys[index] != right_keys[index]:
            return index
    return min(len(left_keys), len(right_keys))


def _drop_leading(units: Sequence[TranslationUnit], count: int) -> tuple[TranslationUnit, ...]:
    if count <= 0:
        return tuple(units)
    positions = _positions(units)
    if not positions:
        return tuple(units)
    boundary = positions[-1] + 1 if count >= len(positions) else positions[count - 1] + 1
    return tuple(units[boundary:])


def _take_through(
    units: Sequence[TranslationUnit],
    count: int,
) -> tuple[tuple[TranslationUnit, ...], tuple[TranslationUnit, ...]]:
    if count <= 0:
        return (), tuple(units)
    positions = _positions(units)
    if not positions:
        return (), tuple(units)
    boundary = positions[-1] + 1 if count >= len(positions) else positions[count - 1] + 1
    return tuple(units[:boundary]), tuple(units[boundary:])


def _with_boundary(
    units: Sequence[TranslationUnit],
    committed_text: str,
) -> tuple[TranslationUnit, ...]:
    if not units or not committed_text:
        return tuple(units)
    first = units[0]
    if first.prefix or first.kind != "lexical" or committed_text[-1].isspace():
        return tuple(units)
    if committed_text[-1].isascii() and committed_text[-1].isalnum():
        return (replace(first, prefix=" "), *units[1:])
    return tuple(units)


class TranslationSnapshotStabilizer:
    """Append-only target prefix with a replaceable translated tail."""

    def __init__(
        self,
        *,
        history_size: int = 3,
        agreement_rounds: int = 2,
        guard_units: int = 6,
        divergence_rounds: int = 2,
        anchor_units: int = 12,
        min_anchor_units: int = 3,
        debug_trace: bool = False,
    ) -> None:
        if history_size < agreement_rounds or agreement_rounds < 1:
            raise ValueError("history_size must be >= agreement_rounds >= 1")
        if guard_units < 0:
            raise ValueError("guard_units must be non-negative")
        if divergence_rounds < 2:
            raise ValueError("divergence_rounds must be >= 2")
        if anchor_units < min_anchor_units or min_anchor_units < 1:
            raise ValueError("anchor_units must be >= min_anchor_units >= 1")

        self.history_size = history_size
        self.agreement_rounds = agreement_rounds
        self.guard_units = guard_units
        self.divergence_rounds = divergence_rounds
        self.anchor_units = anchor_units
        self.min_anchor_units = min_anchor_units
        self.debug_trace = debug_trace
        self.reset()

    def reset(self) -> None:
        self._committed: list[TranslationUnit] = []
        self._unstable: tuple[TranslationUnit, ...] = ()
        self._history: Deque[tuple[TranslationUnit, ...]] = deque(maxlen=self.history_size)
        self._divergent: Deque[tuple[TranslationUnit, ...]] = deque(
            maxlen=self.divergence_rounds
        )
        self.previous_source_revision: object = None
        self.last_source_text = ""
        self.last_committed_source_boundary = 0
        self._last_source_signature: Optional[tuple[str, str]] = None
        self._last_stable_source_units = 0
        self._last_total_source_units = 0
        self._finalized = False

    @property
    def committed_translation(self) -> str:
        return render_translation(self._committed)

    @property
    def unstable_translation(self) -> str:
        return render_translation(self._unstable)

    @property
    def display_translation(self) -> str:
        return self.committed_translation + self.unstable_translation

    def observe(
        self,
        translation_snapshot: str,
        source_revision: object,
        source_text: str,
        *,
        stable_source_text: str = "",
    ) -> TranslationStabilizationUpdate:
        if self._finalized:
            return self._update(False, "finalized", source_revision, "finalized")

        fresh = tuple(tokenize_translation(translation_snapshot))
        if not _keys(fresh):
            update = self._update(False, "empty", source_revision, "empty")
            self._trace(source_text, translation_snapshot, update)
            return update

        signature = ((source_text or "").strip(), (stable_source_text or "").strip())
        if (
            source_revision == self.previous_source_revision
            or signature == self._last_source_signature
        ):
            update = self._update(
                False,
                "duplicate_revision",
                source_revision,
                "duplicate-revision",
            )
            self._trace(source_text, translation_snapshot, update)
            return update

        self.previous_source_revision = source_revision
        self._last_source_signature = signature
        self.last_source_text = signature[0]
        self._last_total_source_units = len(_keys(tokenize_translation(signature[0])))
        self._last_stable_source_units = len(
            _keys(tokenize_translation(signature[1]))
        )

        alignment = self._align(fresh)
        if not alignment.anchored:
            update = self._observe_divergence(alignment, source_revision)
            self._trace(source_text, translation_snapshot, update)
            return update

        candidate = alignment.candidate
        if self._looks_like_outlier(candidate):
            self._divergent.append(candidate)
            if not self._divergent_is_consistent():
                update = self._update(
                    False,
                    "outlier",
                    source_revision,
                    alignment.mode,
                    alignment.anchor,
                )
                self._trace(source_text, translation_snapshot, update)
                return update
            self._history.clear()
            self._history.extend(self._divergent)
            self._divergent.clear()
            status = "divergence"
        else:
            self._divergent.clear()
            self._history.append(candidate)
            status = "normal"

        committed_delta = self._commit_confirmed_prefix()
        self._unstable = self._history[-1] if self._history else candidate
        update = self._update(
            True,
            status,
            source_revision,
            alignment.mode,
            alignment.anchor,
            committed_delta,
        )
        self._trace(source_text, translation_snapshot, update)
        return update

    def finalize(
        self,
        final_translation: Optional[str] = None,
    ) -> TranslationStabilizationUpdate:
        candidate = self._unstable
        alignment_mode = "final-current-tail"
        anchor: tuple[str, ...] = ()
        if final_translation and _keys(tokenize_translation(final_translation)):
            alignment = self._align(tuple(tokenize_translation(final_translation)))
            alignment_mode = "final-" + alignment.mode
            anchor = alignment.anchor
            if alignment.anchored:
                candidate = alignment.candidate
            elif self._compatible(candidate, alignment.candidate):
                candidate = alignment.candidate
                alignment_mode = "final-divergent-branch"

        candidate = _with_boundary(candidate, self.committed_translation)
        committed_delta = render_translation(candidate)
        self._committed.extend(candidate)
        self._unstable = ()
        self._history.clear()
        self._divergent.clear()
        self._finalized = True
        update = self._update(
            True,
            "finalize",
            self.previous_source_revision,
            alignment_mode,
            anchor,
            committed_delta,
        )
        self._trace(self.last_source_text, final_translation or "", update)
        return update

    def _align(self, fresh: tuple[TranslationUnit, ...]) -> _Alignment:
        committed_keys = _keys(self._committed)
        fresh_keys = _keys(fresh)
        if not committed_keys:
            return _Alignment("bootstrap", fresh)

        if fresh_keys[: len(committed_keys)] == committed_keys:
            return _Alignment(
                "full-prefix",
                self._candidate_after(fresh, len(committed_keys)),
                tuple(committed_keys[-self.anchor_units:]),
            )

        max_overlap = min(self.anchor_units, len(committed_keys), len(fresh_keys))
        for overlap in range(max_overlap, self.min_anchor_units - 1, -1):
            tail = committed_keys[-overlap:]
            if fresh_keys[:overlap] == tail:
                return _Alignment(
                    "suffix-prefix",
                    self._candidate_after(fresh, overlap),
                    tuple(tail),
                )
        for overlap in range(max_overlap, self.min_anchor_units - 1, -1):
            tail = committed_keys[-overlap:]
            for start in range(1, len(fresh_keys) - overlap + 1):
                if fresh_keys[start:start + overlap] == tail:
                    return _Alignment(
                        "interior-anchor",
                        self._candidate_after(fresh, start + overlap),
                        tuple(tail),
                    )
        return _Alignment(
            "divergent",
            _with_boundary(fresh, self.committed_translation),
            anchored=False,
        )

    def _candidate_after(
        self,
        fresh: tuple[TranslationUnit, ...],
        matched_count: int,
    ) -> tuple[TranslationUnit, ...]:
        positions = _positions(fresh)
        if matched_count <= 0:
            return fresh
        if matched_count > len(positions):
            return ()
        boundary = positions[matched_count - 1] + 1
        candidate = tuple(fresh[boundary:])
        if self._committed and self._committed[-1].kind == "punctuation":
            while candidate and candidate[0].kind == "punctuation":
                candidate = candidate[1:]
        return _with_boundary(candidate, self.committed_translation)

    def _looks_like_outlier(self, candidate: tuple[TranslationUnit, ...]) -> bool:
        if not self._unstable:
            return False
        old_count = len(_keys(self._unstable))
        new_count = len(_keys(candidate))
        return min(old_count, new_count) >= 3 and _prefix_length(self._unstable, candidate) == 0

    def _compatible(
        self,
        left: Sequence[TranslationUnit],
        right: Sequence[TranslationUnit],
    ) -> bool:
        if not _keys(left) or not _keys(right):
            return False
        common = _prefix_length(left, right)
        return common >= min(3, len(_keys(left)), len(_keys(right)))

    def _divergent_is_consistent(self) -> bool:
        return (
            len(self._divergent) >= self.divergence_rounds
            and all(
                self._compatible(self._divergent[0], item)
                for item in list(self._divergent)[1:]
            )
        )

    def _observe_divergence(
        self,
        alignment: _Alignment,
        source_revision: object,
    ) -> TranslationStabilizationUpdate:
        self._divergent.append(alignment.candidate)
        if not self._divergent_is_consistent():
            return self._update(
                False,
                "outlier",
                source_revision,
                alignment.mode,
            )
        if self._committed:
            # A rewritten full target with no committed-tail anchor has no
            # trustworthy cut point. Appending it would duplicate the already
            # committed translation, so retain the last safe mutable tail.
            self._history.clear()
            self._divergent.clear()
            return self._update(
                False,
                "divergence",
                source_revision,
                "divergence-unanchored",
            )
        self._unstable = self._divergent[-1]
        self._history.clear()
        self._divergent.clear()
        return self._update(
            True,
            "divergence",
            source_revision,
            "divergence-recovery",
        )

    def _commit_confirmed_prefix(self) -> str:
        if len(self._history) < self.agreement_rounds:
            return ""
        if self._last_stable_source_units <= self.last_committed_source_boundary:
            return ""

        recent = list(self._history)[-self.agreement_rounds:]
        agreement = len(_keys(recent[0]))
        for candidate in recent[1:]:
            agreement = min(agreement, _prefix_length(recent[0], candidate))

        total_target_units = len(_keys(self._committed)) + len(_keys(recent[-1]))
        if self._last_total_source_units <= 0:
            return ""
        supported_total = int(
            total_target_units
            * min(1.0, self._last_stable_source_units / self._last_total_source_units)
        )
        supported_remainder = max(0, supported_total - len(_keys(self._committed)))
        commit_count = min(
            max(0, agreement - self.guard_units),
            supported_remainder,
        )
        if commit_count <= 0:
            return ""

        committed_units, _ = _take_through(recent[-1], commit_count)
        committed_units = _with_boundary(committed_units, self.committed_translation)
        delta = render_translation(committed_units)
        self._committed.extend(committed_units)
        self._history = deque(
            (_drop_leading(candidate, commit_count) for candidate in self._history),
            maxlen=self.history_size,
        )
        self.last_committed_source_boundary = self._last_stable_source_units
        return delta

    def _update(
        self,
        accepted: bool,
        status: str,
        source_revision: object,
        alignment_mode: str,
        matched_anchor: tuple[str, ...] = (),
        committed_delta: str = "",
    ) -> TranslationStabilizationUpdate:
        committed = self.committed_translation
        unstable = self.unstable_translation
        return TranslationStabilizationUpdate(
            committed_delta=committed_delta,
            committed_translation=committed,
            unstable_translation=unstable,
            display_translation=committed + unstable,
            accepted=accepted,
            status=status,
            source_revision=source_revision,
            alignment_mode=alignment_mode,
            matched_anchor=matched_anchor,
        )

    def _trace(
        self,
        source_text: str,
        raw_translation: str,
        update: TranslationStabilizationUpdate,
    ) -> None:
        if not self.debug_trace:
            return
        logger.info(
            "[qwen-translation] [TRANSLATION SOURCE]=%r [RAW TRANSLATION]=%r "
            "[SOURCE REVISION]=%r [ALIGNMENT]=%s anchor=%r "
            "[COMMITTED DELTA]=%r [COMMITTED TRANSLATION]=%r "
            "[UNSTABLE TRANSLATION]=%r [DISPLAY TRANSLATION]=%r [STATUS]=%s",
            source_text,
            raw_translation,
            update.source_revision,
            update.alignment_mode,
            "".join(update.matched_anchor),
            update.committed_delta,
            update.committed_translation,
            update.unstable_translation,
            update.display_translation,
            update.status,
        )
