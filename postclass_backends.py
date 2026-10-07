"""Explicit post-class backend selection; no provider fallback."""
from __future__ import annotations

import os
from dataclasses import replace

from agent1_v1 import ModelSpec


def selected_spec(stage: str, backend: str, qwen: ModelSpec) -> ModelSpec:
    if backend not in {"qwen", "codex"}:
        raise ValueError("Execution backend must be qwen or codex")
    if backend == "qwen":
        # A Qwen selection must not inherit an unrelated provider/model.
        model = qwen.model if qwen.provider == "alibaba_cloud_model_studio" else "qwen3.8-max"
        return replace(qwen, provider="alibaba_cloud_model_studio", model=model)
    prefix = {"agent1": "AGENT1", "asr": "AGENT2", "enrichment": "AGENT2_ENRICHMENT"}[stage]
    model = (os.getenv(f"{prefix}_CODEX_MODEL") or os.getenv("POSTCLASS_CODEX_MODEL")
             or os.getenv("PRE_AGENT_CODEX_MODEL") or "gpt-6.1-sol")
    if model == "default":
        raise ValueError("Codex requires an explicit model for saved run identity")
    # Keep Codex's budget separate from Qwen's larger configured context window.
    limit = int(os.getenv(f"{prefix}_CODEX_CONTEXT_WINDOW_TOKENS")
                or os.getenv("POSTCLASS_CODEX_CONTEXT_WINDOW_TOKENS") or "200000")
    if limit <= 0:
        raise ValueError("Codex context budget must be positive")
    return replace(qwen, provider="codex", model=model, context_window_tokens=limit)
