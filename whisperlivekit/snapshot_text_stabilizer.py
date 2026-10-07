"""Stabilize full-text ASR snapshots without relying on token timestamps.

The Qwen HTTP ASR endpoint returns a complete, revisable transcript on every
request.  This module keeps the permanently committed prefix append-only and
allows the remaining preview to be replaced wholesale as later snapshots
correct it.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
import logging
import re
import unicodedata
from typing import Deque, Iterable, Literal, Optional, Sequence


logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"\w+(?:[\u2019']\w+)*(?:-\w+(?:[\u2019']\w+)*)*|[^\w\s]", re.UNICODE)


@dataclass(frozen=True)
class SnapshotToken:
    """One raw word or punctuation unit with lossless leading whitespace."""

    raw: str
    normalized: str
    kind: Literal["word", "punctuation"]
    prefix: str = ""

    @property
    def surface(self) -> str:
        return self.prefix + self.raw


@dataclass(frozen=True)
class SnapshotStabilizationUpdate:
    """Public state produced after one snapshot or explicit flush."""

    committed_delta: str
    committed_source: str
    unstable_source: str
    display_source: str
    accepted: bool
    alignment_mode: str
    matched_anchor: tuple[str, ...] = ()
    ignored_reason: Optional[str] = None
    finalized: bool = False


@dataclass(frozen=True)
class _Alignment:
    mode: str
    candidate: tuple[SnapshotToken, ...]
    anchor: tuple[str, ...] = ()
    anchored: bool = True


def _normalize_word(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return value.replace("\u2019", "'")


def tokenize_snapshot(text: str) -> list[SnapshotToken]:
    """Split English-like text into word and punctuation units losslessly."""

    raw_text = (text or "").strip()
    tokens: list[SnapshotToken] = []
    previous_end = 0
    for match in _TOKEN_RE.finditer(raw_text):
        raw = match.group(0)
        kind: Literal["word", "punctuation"] = (
            "word" if any(char.isalnum() or char == "_" for char in raw) else "punctuation"
        )
        normalized = (
            _normalize_word(raw)
            if kind == "word"
            else unicodedata.normalize("NFKC", raw)
        )
        tokens.append(
            SnapshotToken(
                raw=raw,
                normalized=normalized,
                kind=kind,
                prefix=raw_text[previous_end:match.start()],
            )
        )
        previous_end = match.end()
    return tokens


def render_snapshot(tokens: Iterable[SnapshotToken]) -> str:
    return "".join(token.surface for token in tokens)


def _word_positions(tokens: Sequence[SnapshotToken]) -> list[int]:
    return [index for index, token in enumerate(tokens) if token.kind == "word"]


def _word_keys(tokens: Sequence[SnapshotToken]) -> list[str]:
    return [token.normalized for token in tokens if token.kind == "word"]


def _leading_word_prefix_length(
    left: Sequence[SnapshotToken],
    right: Sequence[SnapshotToken],
) -> int:
    left_words = _word_keys(left)
    right_words = _word_keys(right)
    limit = min(len(left_words), len(right_words))
    for index in range(limit):
        if left_words[index] != right_words[index]:
            return index
    return limit


def _drop_leading_words(
    tokens: Sequence[SnapshotToken],
    word_count: int,
) -> tuple[SnapshotToken, ...]:
    if word_count <= 0:
        return tuple(tokens)
    positions = _word_positions(tokens)
    if word_count >= len(positions):
        boundary = positions[-1] + 1 if positions else 0
        return tuple(tokens[boundary:])
    boundary = positions[word_count - 1] + 1
    return tuple(tokens[boundary:])


def _take_through_words(
    tokens: Sequence[SnapshotToken],
    word_count: int,
) -> tuple[tuple[SnapshotToken, ...], tuple[SnapshotToken, ...]]:
    """Split immediately after the Nth word, keeping trailing punctuation mutable."""

    if word_count <= 0:
        return (), tuple(tokens)
    positions = _word_positions(tokens)
    if not positions:
        return (), tuple(tokens)
    if word_count >= len(positions):
        boundary = positions[-1] + 1
    else:
        boundary = positions[word_count - 1] + 1
    return tuple(tokens[:boundary]), tuple(tokens[boundary:])


def _with_leading_boundary(
    tokens: Sequence[SnapshotToken],
    committed_text: str,
) -> tuple[SnapshotToken, ...]:
    if not tokens or not committed_text:
        return tuple(tokens)
    first = tokens[0]
    if first.prefix or first.kind == "punctuation" or committed_text[-1].isspace():
        return tuple(tokens)
    return (replace(first, prefix=" "), *tokens[1:])


class SnapshotTextStabilizer:
    """Append-only text stabilizer for revisable full ASR snapshots."""

    def __init__(
        self,
        *,
        history_size: int = 3,
        agreement_rounds: int = 2,
        guard_words: int = 2,
        anchor_words: int = 8,
        min_anchor_words: int = 2,
        divergence_rounds: int = 2,
        debug_trace: bool = False,
    ) -> None:
        if history_size < agreement_rounds or agreement_rounds < 1:
            raise ValueError("history_size must be >= agreement_rounds >= 1")
        if guard_words < 0:
            raise ValueError("guard_words must be non-negative")
        if anchor_words < min_anchor_words or min_anchor_words < 1:
            raise ValueError("anchor_words must be >= min_anchor_words >= 1")
        if divergence_rounds < 2:
            raise ValueError("divergence_rounds must be >= 2")

        self.history_size = history_size
        self.agreement_rounds = agreement_rounds
        self.guard_words = guard_words
        self.anchor_words = anchor_words
        self.min_anchor_words = min_anchor_words
        self.divergence_rounds = divergence_rounds
        self.debug_trace = debug_trace
        self.reset()

    def reset(self) -> None:
        self._committed: list[SnapshotToken] = []
        self._unstable: tuple[SnapshotToken, ...] = ()
        self._history: Deque[tuple[SnapshotToken, ...]] = deque(
            maxlen=self.history_size
        )
        self._history_revisions: Deque[object] = deque(maxlen=self.history_size)
        self._divergent: Deque[tuple[SnapshotToken, ...]] = deque(
            maxlen=self.divergence_rounds
        )
        self._last_audio_revision: object = object()
        self._finalized = False

    def reset_window(self) -> None:
        """Forget per-window evidence while retaining append-only public text."""

        self._history.clear()
        self._history_revisions.clear()
        self._divergent.clear()

    @property
    def committed_source(self) -> str:
        return render_snapshot(self._committed)

    @property
    def unstable_source(self) -> str:
        return render_snapshot(self._unstable)

    @property
    def display_source(self) -> str:
        return self.committed_source + self.unstable_source

    def observe(
        self,
        snapshot_text: str,
        *,
        audio_revision: object = None,
    ) -> SnapshotStabilizationUpdate:
        if self._finalized:
            return self._update(
                accepted=False,
                alignment_mode="finalized",
                ignored_reason="finalized",
            )

        fresh = tuple(tokenize_snapshot(snapshot_text))
        if not _word_keys(fresh):
            update = self._update(
                accepted=False,
                alignment_mode="empty",
                ignored_reason="empty-snapshot",
            )
            self._trace(snapshot_text, update)
            return update

        alignment = self._align(fresh)
        if not alignment.anchored:
            update = self._observe_divergence(snapshot_text, alignment, audio_revision)
            self._trace(snapshot_text, update)
            return update

        candidate = alignment.candidate
        if self._looks_like_single_outlier(candidate):
            self._divergent.append(candidate)
            if not self._divergent_is_consistent():
                update = self._update(
                    accepted=False,
                    alignment_mode=alignment.mode,
                    matched_anchor=alignment.anchor,
                    ignored_reason="single-outlier",
                )
                self._trace(snapshot_text, update)
                return update
            self._history.clear()
            self._history_revisions.clear()
            for branch in self._divergent:
                self._history.append(branch)
                self._history_revisions.append(object())
            self._divergent.clear()
            self._last_audio_revision = audio_revision
        else:
            self._divergent.clear()
            if (
                audio_revision is not None
                and audio_revision == self._last_audio_revision
                and self._history
            ):
                self._history[-1] = candidate
            else:
                self._history.append(candidate)
                self._history_revisions.append(audio_revision)
            self._last_audio_revision = audio_revision

        committed_delta = self._commit_confirmed_prefix()
        self._unstable = self._history[-1] if self._history else candidate
        update = self._update(
            committed_delta=committed_delta,
            accepted=True,
            alignment_mode=alignment.mode,
            matched_anchor=alignment.anchor,
        )
        self._trace(snapshot_text, update)
        return update

    def finalize(self, final_snapshot: Optional[str] = None) -> SnapshotStabilizationUpdate:
        """Flush the best aligned mutable tail without revising committed text."""

        mode = "final-current-tail"
        anchor: tuple[str, ...] = ()
        candidate = self._unstable
        if final_snapshot and _word_keys(tokenize_snapshot(final_snapshot)):
            alignment = self._align(tuple(tokenize_snapshot(final_snapshot)))
            mode = "final-" + alignment.mode
            anchor = alignment.anchor
            if alignment.anchored:
                candidate = alignment.candidate
            elif self._candidate_compatible(candidate, alignment.candidate):
                candidate = alignment.candidate
                mode = "final-divergent-branch"

        candidate = _with_leading_boundary(candidate, self.committed_source)
        committed_delta = render_snapshot(candidate)
        if candidate:
            self._committed.extend(candidate)
        self._unstable = ()
        self._history.clear()
        self._history_revisions.clear()
        self._divergent.clear()
        self._finalized = True
        update = self._update(
            committed_delta=committed_delta,
            accepted=True,
            alignment_mode=mode,
            matched_anchor=anchor,
            finalized=True,
        )
        self._trace(final_snapshot or "", update)
        return update

    def _align(self, fresh: tuple[SnapshotToken, ...]) -> _Alignment:
        committed_words = _word_keys(self._committed)
        fresh_words = _word_keys(fresh)
        if not committed_words:
            return _Alignment("bootstrap", fresh)

        if len(fresh_words) >= len(committed_words) and (
            fresh_words[: len(committed_words)] == committed_words
        ):
            return _Alignment(
                "full-prefix",
                self._candidate_after_matched_words(fresh, len(committed_words)),
                tuple(committed_words[-min(len(committed_words), self.anchor_words):]),
            )

        max_overlap = min(self.anchor_words, len(committed_words), len(fresh_words))
        for overlap in range(max_overlap, self.min_anchor_words - 1, -1):
            tail = committed_words[-overlap:]
            if fresh_words[:overlap] == tail:
                return _Alignment(
                    "suffix-prefix",
                    self._candidate_after_matched_words(fresh, overlap),
                    tuple(tail),
                )

        for overlap in range(max_overlap, self.min_anchor_words - 1, -1):
            tail = committed_words[-overlap:]
            for start in range(1, len(fresh_words) - overlap + 1):
                if fresh_words[start:start + overlap] == tail:
                    return _Alignment(
                        "interior-anchor",
                        self._candidate_after_matched_words(
                            fresh, start + overlap
                        ),
                        tuple(tail),
                    )

        return _Alignment(
            "divergent",
            _with_leading_boundary(fresh, self.committed_source),
            anchored=False,
        )

    def _candidate_after_matched_words(
        self,
        fresh: tuple[SnapshotToken, ...],
        matched_word_count: int,
    ) -> tuple[SnapshotToken, ...]:
        positions = _word_positions(fresh)
        if matched_word_count <= 0:
            return fresh
        if matched_word_count > len(positions):
            return ()
        boundary = positions[matched_word_count - 1] + 1
        candidate = tuple(fresh[boundary:])

        # If committed text already owns punctuation at the seam, do not emit
        # another punctuation mark from the overlapping snapshot.
        if self._committed and self._committed[-1].kind == "punctuation":
            while candidate and candidate[0].kind == "punctuation":
                candidate = candidate[1:]
        return _with_leading_boundary(candidate, self.committed_source)

    def _looks_like_single_outlier(
        self,
        candidate: tuple[SnapshotToken, ...],
    ) -> bool:
        if not self._unstable:
            return False
        old_words = _word_keys(self._unstable)
        new_words = _word_keys(candidate)
        if min(len(old_words), len(new_words)) < 2:
            return False
        return _leading_word_prefix_length(self._unstable, candidate) == 0

    def _candidate_compatible(
        self,
        left: Sequence[SnapshotToken],
        right: Sequence[SnapshotToken],
    ) -> bool:
        left_words = _word_keys(left)
        right_words = _word_keys(right)
        if not left_words or not right_words:
            return False
        common = _leading_word_prefix_length(left, right)
        return common >= min(2, len(left_words), len(right_words))

    def _divergent_is_consistent(self) -> bool:
        return (
            len(self._divergent) >= self.divergence_rounds
            and all(
                self._candidate_compatible(self._divergent[0], item)
                for item in list(self._divergent)[1:]
            )
        )

    def _observe_divergence(
        self,
        snapshot_text: str,
        alignment: _Alignment,
        audio_revision: object,
    ) -> SnapshotStabilizationUpdate:
        self._divergent.append(alignment.candidate)
        if not self._divergent_is_consistent():
            return self._update(
                accepted=False,
                alignment_mode=alignment.mode,
                ignored_reason="single-outlier",
            )

        # Adopt a repeated branch only as mutable preview. Without an anchor it
        # is not safe to append any part of it permanently.
        self._unstable = self._divergent[-1]
        self._history.clear()
        self._history_revisions.clear()
        self._last_audio_revision = audio_revision
        self._divergent.clear()
        return self._update(
            accepted=True,
            alignment_mode="divergence-recovery",
            ignored_reason=None,
        )

    def _commit_confirmed_prefix(self) -> str:
        if len(self._history) < self.agreement_rounds:
            return ""
        recent = list(self._history)[-self.agreement_rounds:]
        stable_words = len(_word_keys(recent[0]))
        for candidate in recent[1:]:
            stable_words = min(
                stable_words,
                _leading_word_prefix_length(recent[0], candidate),
            )
        commit_words = max(0, stable_words - self.guard_words)
        if commit_words <= 0:
            return ""

        committed_units, _ = _take_through_words(recent[-1], commit_words)
        committed_units = _with_leading_boundary(
            committed_units,
            self.committed_source,
        )
        delta = render_snapshot(committed_units)
        self._committed.extend(committed_units)

        rebased: list[tuple[SnapshotToken, ...]] = []
        for candidate in self._history:
            rebased.append(_drop_leading_words(candidate, commit_words))
        self._history = deque(rebased, maxlen=self.history_size)
        return delta

    def _update(
        self,
        *,
        committed_delta: str = "",
        accepted: bool,
        alignment_mode: str,
        matched_anchor: tuple[str, ...] = (),
        ignored_reason: Optional[str] = None,
        finalized: bool = False,
    ) -> SnapshotStabilizationUpdate:
        committed = self.committed_source
        unstable = self.unstable_source
        return SnapshotStabilizationUpdate(
            committed_delta=committed_delta,
            committed_source=committed,
            unstable_source=unstable,
            display_source=committed + unstable,
            accepted=accepted,
            alignment_mode=alignment_mode,
            matched_anchor=matched_anchor,
            ignored_reason=ignored_reason,
            finalized=finalized,
        )

    def _trace(
        self,
        raw_snapshot: str,
        update: SnapshotStabilizationUpdate,
    ) -> None:
        if not self.debug_trace:
            return
        logger.info(
            "[qwen-snapshot] raw=%r alignment=%s anchor=%r delta=%r "
            "committed=%r unstable=%r translation_source=%r accepted=%s reason=%s",
            raw_snapshot,
            update.alignment_mode,
            " ".join(update.matched_anchor),
            update.committed_delta,
            update.committed_source,
            update.unstable_source,
            update.display_source,
            update.accepted,
            update.ignored_reason,
        )
