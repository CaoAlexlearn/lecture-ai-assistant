from __future__ import annotations

import unittest

from token_buffer import DeterministicTokenCounter, TokenBuffer
from transcript import FinalTranscript, SourceFinalTranscript

CHINESE_SENTINEL = "中文哨兵绝对不能进入Agent1"


def sentence(sentence_id: int, source: str) -> FinalTranscript:
    return FinalTranscript(
        session_id="session-1",
        sentence_id=sentence_id,
        source_text=source,
        translated_text=CHINESE_SENTINEL,
        start_ms=sentence_id * 100,
        end_ms=sentence_id * 100 + 90,
    )


def words(prefix: str, count: int) -> str:
    return " ".join(f"{prefix}{index}" for index in range(count))


class SpyTokenCounter:
    def __init__(self) -> None:
        self.delegate = DeterministicTokenCounter()
        self.inputs: list[str] = []

    def count(self, text: str) -> int:
        self.inputs.append(text)
        return self.delegate.count(text)

    def tail(self, text: str, max_tokens: int) -> str:
        self.inputs.append(text)
        return self.delegate.tail(text, max_tokens)


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class TokenBufferTests(unittest.TestCase):
    def make_buffer(
        self,
        *,
        target: int = 1_800,
        overlap: int = 400,
        counter: SpyTokenCounter | DeterministicTokenCounter | None = None,
    ) -> TokenBuffer:
        return TokenBuffer(
            course_id="course",
            lecture_id="lecture",
            session_id="session-1",
            token_counter=counter or DeterministicTokenCounter(),
            token_target=target,
            overlap_tokens=overlap,
        )

    def test_append_never_makes_a_segmentation_decision(self) -> None:
        buffer = self.make_buffer()
        self.assertIsNone(buffer.append(sentence(1, words("a", 1_700))))
        self.assertIsNone(buffer.append(sentence(2, words("b", 150))))
        self.assertEqual(buffer.pending_token_count, 1_850)
        self.assertEqual(buffer.pending_sentence_count, 2)

    def test_resumed_session_continues_block_sequence(self) -> None:
        buffer = TokenBuffer(
            course_id="course",
            lecture_id="lecture",
            session_id="session-1",
            token_counter=DeterministicTokenCounter(),
            token_target=1,
            initial_block_sequence=7,
        )

        buffer.append(sentence(1, "next"))
        job = buffer.flush()

        assert job is not None
        self.assertEqual(job.block_id, "session-1:B000008")

    def test_single_sentence_over_target_is_never_split(self) -> None:
        source = words("long", 1_801)
        buffer = self.make_buffer()
        self.assertIsNone(buffer.append(sentence(1, source)))
        job = buffer.flush()

        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job.current_chunk_token_count, 1_801)
        self.assertEqual(job.current_chunk_text, source)
        self.assertEqual(job.source_sentence_ids, (1,))

    def test_flush_emits_742_token_tail(self) -> None:
        buffer = self.make_buffer()
        self.assertIsNone(buffer.append(sentence(1, words("tail", 742))))

        job = buffer.flush()

        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job.current_chunk_token_count, 742)
        self.assertEqual(job.current_chunk_text, words("tail", 742))
        self.assertIsNone(buffer.flush())

    def test_prefix_consumption_retains_ordered_suffix_and_prefix_overlap(self) -> None:
        buffer = self.make_buffer(target=5, overlap=2)
        for index in range(1, 11):
            buffer.append(sentence(index, f"word{index}"))
        first = buffer.consume_prefix(6)
        self.assertEqual(first.source_sentence_ids, tuple(range(1, 7)))
        self.assertEqual(
            tuple(item.sentence_id for item in buffer.snapshot_sentences()),
            tuple(range(7, 11)),
        )
        second = buffer.flush()

        self.assertIsNotNone(second)
        assert second is not None
        self.assertEqual(second.previous_overlap_text, "word5\nword6")
        self.assertEqual(second.current_chunk_text, "word7\nword8\nword9\nword10")
        self.assertEqual(second.source_sentence_ids, tuple(range(7, 11)))
        self.assertNotIn("word6", second.current_chunk_text)

    def test_token_counter_only_receives_english_source(self) -> None:
        counter = SpyTokenCounter()
        buffer = self.make_buffer(target=3, overlap=1, counter=counter)
        buffer.append(sentence(1, "English source only"))
        job = buffer.flush()

        self.assertIsNotNone(job)
        self.assertTrue(counter.inputs)
        self.assertTrue(all(CHINESE_SENTINEL not in value for value in counter.inputs))

    def test_hard_max_does_not_make_token_buffer_emit(self) -> None:
        buffer = TokenBuffer(
            course_id="course",
            lecture_id="lecture",
            session_id="session-1",
            token_counter=DeterministicTokenCounter(),
            first_target_tokens=10,
            target_tokens=10,
            max_tokens=4,
            time_trigger_min_tokens=1,
            overlap_tokens=1,
        )

        self.assertIsNone(buffer.append(sentence(1, "one two three four")))
        self.assertEqual(buffer.pending_token_count, 4)

    def test_checkpoint_restore_preserves_remaining_timer_window(self) -> None:
        first_clock = FakeClock(100.0)
        original = TokenBuffer(
            course_id="course",
            lecture_id="lecture",
            session_id="session-1",
            token_counter=DeterministicTokenCounter(),
            first_target_tokens=100,
            target_tokens=100,
            first_max_wait_seconds=10,
            max_wait_seconds=10,
            time_trigger_min_tokens=1,
            clock=first_clock,
        )
        self.assertIsNone(
            original.append(
                SourceFinalTranscript(
                    session_id="session-1",
                    sentence_id=1,
                    source_text="timer source",
                    start_ms=100,
                    end_ms=190,
                    source_sequence=1,
                )
            )
        )
        first_clock.now += 7.5
        state = original.export_state()

        restored_clock = FakeClock(500.0)
        restored = TokenBuffer(
            course_id="course",
            lecture_id="lecture",
            session_id="session-1",
            token_counter=DeterministicTokenCounter(),
            first_target_tokens=100,
            target_tokens=100,
            first_max_wait_seconds=10,
            max_wait_seconds=10,
            time_trigger_min_tokens=1,
            clock=restored_clock,
        )
        restored.restore_state(state)

        self.assertAlmostEqual(restored.seconds_until_time_trigger() or 0, 2.5)
        self.assertIsNone(restored.flush_for_time_trigger())
        restored_clock.now += 2.5
        job = restored.flush_for_time_trigger()
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job.source_sentence_ids, (1,))
        self.assertEqual(job.block_id, "session-1:B000001")


if __name__ == "__main__":
    unittest.main()
