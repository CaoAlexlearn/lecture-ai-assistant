"""Shared production/Lab context builders for configurable agents."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from token_buffer import DeterministicTokenCounter


@dataclass(frozen=True, slots=True)
class ContextBlock:
    key: str
    label: str
    role: str
    content: str
    sent_to_model: bool = True
    scope: str | None = None
    version_id: str | None = None
    source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        counter = DeterministicTokenCounter()
        value = {
            "key": self.key,
            "label": self.label,
            "role": self.role,
            "content": self.content,
            "characterCount": len(self.content),
            "tokenCount": counter.count(self.content),
            "sentToModel": self.sent_to_model,
        }
        if self.scope is not None:
            value["scope"] = self.scope
        if self.version_id is not None:
            value["versionId"] = self.version_id
        if self.source is not None:
            value["source"] = self.source
        return value


def build_agent1_context_blocks(
    *,
    system_prompt: str,
    previous_context: str,
    transcript_chunk: str,
    instruction_blocks: Sequence[ContextBlock] = (),
) -> tuple[ContextBlock, ...]:
    user_content = (
        "<PREVIOUS_CONTEXT>\n"
        f"{previous_context}\n"
        "</PREVIOUS_CONTEXT>\n\n"
        "<CURRENT_TRANSCRIPT>\n"
        f"{transcript_chunk}\n"
        "</CURRENT_TRANSCRIPT>"
    )
    return (
        ContextBlock("system_prompt", "System Prompt", "system", system_prompt),
        *instruction_blocks,
        ContextBlock("transcript", "Transcript Context", "user", user_content),
    )


def build_agent1_boundary_context_blocks(
    *,
    system_prompt: str,
    transcript: str,
    hard_max_reached: bool = False,
) -> tuple[ContextBlock, ...]:
    """Build the exact message pair used by the production boundary detector."""

    hard_max_instruction = (
        "\n\nHARD MAX REACHED. Choose the best valid semantic boundary. "
        "Return WAIT only if no valid boundary exists."
        if hard_max_reached
        else ""
    )
    return (
        ContextBlock("system_prompt", "System Prompt", "system", system_prompt),
        ContextBlock(
            "boundary_transcript",
            "Finalized source sentence snapshot",
            "user",
            (
                "Select a completed semantic prefix from these finalized English "
                f"source sentences.{hard_max_instruction}\n\n{transcript}"
            ),
        ),
    )


def build_pre_agent_context_blocks(
    *,
    system_prompt: str,
    task: str,
    normalized_path: str,
    instruction_blocks: Sequence[ContextBlock] = (),
) -> tuple[ContextBlock, ...]:
    return (
        ContextBlock("system_prompt", "System Prompt", "system", system_prompt),
        *instruction_blocks,
        ContextBlock("task", "Production Task", "user", task),
        ContextBlock(
            "normalized_source",
            "Tool-accessible normalized evidence",
            "workspace",
            normalized_path,
            sent_to_model=False,
        ),
    )


def model_messages(blocks: tuple[ContextBlock, ...]) -> list[dict[str, str]]:
    return [
        {"role": block.role, "content": block.content}
        for block in blocks
        if block.sent_to_model and block.role in {"system", "user", "assistant"}
    ]
