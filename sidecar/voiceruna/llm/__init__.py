"""LLM layer: OpenAI-compatible adapter, provider cascade and the system prompt.

Public contract (docs/CONTRACTS.md):
    LLMResult, CascadeClient, build_system_prompt, ProviderError
"""

from __future__ import annotations

from .adapter import Completion, ProviderAdapter, ProviderError
from .cascade import CascadeClient, LLMResult
from .prompt import build_system_prompt

__all__ = [
    "CascadeClient",
    "Completion",
    "LLMResult",
    "ProviderAdapter",
    "ProviderError",
    "build_system_prompt",
]
