"""Online processor for the full-snapshot Qwen HTTP ASR backend."""

from __future__ import annotations

import logging
import re
import sys
from typing import List, Optional, Tuple

import numpy as np

from whisperlivekit.local_agreement.backends import QwenApiTransientError
from whisperlivekit.snapshot_text_stabilizer import (
    SnapshotStabilizationUpdate,
    SnapshotTextStabilizer,
)
from whisperlivekit.timed_objects import ASRToken, Transcript


logger = logging.getLogger(__name__)
_OUTPUT_CHUNK_RE = re.compile(r"\s*\S+")


class QwenApiSnapshotOnlineProcessor:
    """Adapt Qwen full-text HTTP snapshots to WhisperLiveKit's ASR contract.

    Text stability is decided exclusively by :class:`SnapshotTextStabilizer`.
    Approximate timestamps are assigned only after a source delta is committed.
    """

    SAMPLING_RATE = 16_000
    _WINDOW_OVERLAP_SECONDS = 5.0
    _APPROXIMATE_COMMIT_LAG_SECONDS = 0.75

    def __init__(self, asr, *, debug_trace: bool = False, logfile=sys.stderr):
        self.asr = asr
        self.logfile = logfile
        self.debug_trace = debug_trace
        self.buffer_trimming_sec = float(getattr(asr, "buffer_trimming_sec", 15.0))
        self._language = (
            getattr(asr, "_session_language", None)
            or getattr(asr, "original_language", None)
        )
        self._revision = 0
        self._last_requested_revision: Optional[int] = None
        self._stream_end = 0.0
        self._last_commit_time = 0.0
        self._has_emitted_text = False
        self._reset_utterance(offset=0.0)

    def _new_stabilizer(self) -> SnapshotTextStabilizer:
        return SnapshotTextStabilizer(debug_trace=self.debug_trace)

    def _reset_utterance(self, *, offset: float) -> None:
        self.audio_buffer = np.array([], dtype=np.float32)
        self.buffer_time_offset = float(offset)
        self.stabilizer = self._new_stabilizer()
        self._last_requested_revision = None

    def get_audio_buffer_end_time(self) -> float:
        return self._stream_end

    def insert_audio_chunk(
        self,
        audio: np.ndarray,
        audio_stream_end_time: Optional[float] = None,
    ) -> None:
        self.audio_buffer = np.append(self.audio_buffer, audio.astype(np.float32))
        self._revision += 1
        if audio_stream_end_time is None:
            self._stream_end = self.buffer_time_offset + (
                len(self.audio_buffer) / self.SAMPLING_RATE
            )
        else:
            self._stream_end = float(audio_stream_end_time)

    def process_iter(self) -> Tuple[List[ASRToken], float]:
        if self.audio_buffer.size == 0:
            return [], self._stream_end
        if self._last_requested_revision == self._revision:
            return [], self._stream_end

        try:
            snapshot = self._request_snapshot()
        except QwenApiTransientError as exc:
            # A full-snapshot request is stateless. Keep the last good public
            # state and leave this revision unacknowledged so a later cycle can
            # retry with the same or newer accumulated audio.
            logger.warning(
                "[qwen-snapshot] transient ASR request failed; preserving "
                "committed/unstable state for the next cycle: %s",
                exc,
            )
            return [], self._stream_end
        update = self.stabilizer.observe(
            snapshot,
            audio_revision=self._revision,
        )
        self._last_requested_revision = self._revision
        tokens = self._tokens_for_delta(update.committed_delta, self._stream_end)
        self._maybe_trim_window(update)
        return tokens, self._stream_end

    def get_buffer(self) -> Transcript:
        text = self.stabilizer.unstable_source
        if not text:
            return Transcript(start=None, end=None, text="")
        return Transcript(
            start=self._last_commit_time,
            end=self._stream_end,
            text=text,
            detected_language=self._language,
        )

    def start_silence(self) -> Tuple[List[ASRToken], float]:
        tokens = self._finalize_current_utterance()
        end = self._stream_end
        self._reset_utterance(offset=end)
        return tokens, end

    def end_silence(self, silence_duration: Optional[float], offset: float) -> None:
        if not silence_duration or silence_duration <= 0:
            return
        self._stream_end += float(silence_duration)
        self.buffer_time_offset = self._stream_end
        self._last_commit_time = max(self._last_commit_time, self._stream_end)

    def insert_silence(self, silence_duration: float, offset: float) -> None:
        self.end_silence(silence_duration, offset)

    def new_speaker(self, change_speaker) -> Tuple[List[ASRToken], float]:
        tokens, processed_upto = self.start_silence()
        change_time = getattr(change_speaker, "start", None)
        if change_time is not None:
            self._stream_end = max(self._stream_end, float(change_time))
            self.buffer_time_offset = self._stream_end
            self._last_commit_time = max(self._last_commit_time, self._stream_end)
        return tokens, processed_upto

    def finish(self) -> Tuple[List[ASRToken], float]:
        return self._finalize_current_utterance(), self._stream_end

    def _request_snapshot(self) -> str:
        result = self.asr.transcribe(self.audio_buffer, init_prompt="")
        snapshot_text = getattr(self.asr, "snapshot_text", None)
        if snapshot_text is not None:
            return snapshot_text(result)
        return str(result.get("text", ""))

    def _finalize_current_utterance(self) -> List[ASRToken]:
        if self.audio_buffer.size == 0:
            update = self.stabilizer.finalize()
            return self._tokens_for_delta(
                update.committed_delta,
                self._stream_end,
                final=True,
            )

        observed_delta = ""
        final_snapshot = None
        if self._last_requested_revision != self._revision:
            try:
                final_snapshot = self._request_snapshot()
            except QwenApiTransientError as exc:
                # Finalization must be best-effort: losing the newest snapshot
                # is preferable to leaving the browser stuck waiting forever.
                logger.warning(
                    "[qwen-snapshot] final ASR request failed; flushing the "
                    "last good mutable tail so the session can complete: %s",
                    exc,
                )
            else:
                observed = self.stabilizer.observe(
                    final_snapshot,
                    audio_revision=self._revision,
                )
                observed_delta = observed.committed_delta
                self._last_requested_revision = self._revision

        finalized = self.stabilizer.finalize(final_snapshot)
        combined_delta = observed_delta + finalized.committed_delta
        return self._tokens_for_delta(combined_delta, self._stream_end, final=True)

    def _tokens_for_delta(
        self,
        text: str,
        event_end: float,
        *,
        final: bool = False,
    ) -> List[ASRToken]:
        if not text:
            return []
        if (
            self._has_emitted_text
            and not text[0].isspace()
            and (text[0].isalnum() or text[0] in "'\u2019(\u201c\u2018")
        ):
            text = " " + text

        chunks = [match.group(0) for match in _OUTPUT_CHUNK_RE.finditer(text)]
        if not chunks:
            return []
        start = min(self._last_commit_time, event_end)
        target_end = event_end
        if not final:
            target_end = max(
                start,
                event_end - self._APPROXIMATE_COMMIT_LAG_SECONDS,
            )
        span = max(0.0, target_end - start)
        tokens: list[ASRToken] = []
        for index, chunk in enumerate(chunks):
            token_start = start + span * index / len(chunks)
            token_end = start + span * (index + 1) / len(chunks)
            tokens.append(
                ASRToken(
                    start=token_start,
                    end=token_end,
                    text=chunk,
                    detected_language=self._language,
                )
            )
        self._last_commit_time = max(self._last_commit_time, target_end)
        self._has_emitted_text = True
        return tokens

    def _maybe_trim_window(self, update: SnapshotStabilizationUpdate) -> None:
        duration = len(self.audio_buffer) / self.SAMPLING_RATE
        if not update.committed_delta or duration <= self.buffer_trimming_sec:
            return
        keep_seconds = min(
            self._WINDOW_OVERLAP_SECONDS,
            max(2.0, self.buffer_trimming_sec / 2.0),
        )
        keep_samples = int(keep_seconds * self.SAMPLING_RATE)
        if keep_samples >= len(self.audio_buffer):
            return
        dropped_samples = len(self.audio_buffer) - keep_samples
        self.audio_buffer = self.audio_buffer[-keep_samples:]
        self.buffer_time_offset += dropped_samples / self.SAMPLING_RATE
        self.stabilizer.reset_window()
        logger.debug(
            "[qwen-snapshot] trimmed audio window to %.2fs with committed tail anchor",
            keep_seconds,
        )
