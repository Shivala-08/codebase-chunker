"""NVIDIA NIM client (v4: thin back-compat shim over the provider chain).

v0–v3 this was the whole inference layer; v4 moves failover, per-provider
rate limiting, and circuit breaking into `codegraph.providers` (backed by
providers.yaml). Everything here re-exports with the same signatures so
agent.py, docgen.py, main.py and the tests are untouched. NIM stays the
primary provider — it's simply first in the chain.
"""
from __future__ import annotations

import os

from . import providers
from .providers import (
    AllProvidersExhausted,
    complete,
    explain_node,
    chat_answer,
    get_providers,
    last_provider_status,
    reset_providers,
)

__all__ = [
    "AllProvidersExhausted",
    "complete",
    "complete_raw",
    "explain_node",
    "chat_answer",
    "get_providers",
    "last_provider_status",
    "reset_providers",
]

# Legacy names kept for callers that reference nim.MODEL_CHAT (agent.py,
# docgen.py). The provider chain resolves model ids per provider at call time
# — these constants are only used by old code paths.
MODEL_EXPLAIN = os.environ.get("CODEGRAPH_MODEL_EXPLAIN", "nvidia/nemotron-3.5-lightning-30b-a3b")
MODEL_CHAT = os.environ.get("CODEGRAPH_MODEL_CHAT", "nvidia/nemotron-3-super-120b-a12b")


def complete_raw(system: str, user: str, tier: str = "chat",
                 max_tokens: int | None = None) -> str:
    """v3 contract: return just the text (tests stub this with a string).

    For provider attribution use providers.complete_raw, which returns
    (text, provider_name).
    """
    text, _provider = providers.complete_raw(
        system=system, user=user, tier=tier, max_tokens=max_tokens)
    return text
