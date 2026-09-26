"""Provider fallback chain (v4 TRD §3).

Replaces the single-NIM client: a list of OpenAI-compatible providers loaded
from providers.yaml, tried in config order. Per-provider rate limiter + circuit
breaker so a 429 from one provider never slows the next attempt in the same
request. `complete()` returns (text, provider_name) so callers can surface
which provider actually served the request (user story #4).

Module-level API mirrors nim.py (explain_node / chat_answer / complete_raw)
so main.py, agent.py and docgen.py keep working; the nim module re-exports.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from openai import OpenAI

_DEFAULT_CONFIG_PATH = Path(__file__).parent / "providers.yaml"


def _config_path() -> Path:
    """Resolved per call (not at import) so tests can repoint it via env."""
    return Path(os.environ.get("CODEGRAPH_PROVIDERS_YAML", _DEFAULT_CONFIG_PATH))

# A provider whose key env var is unset is skipped at call time (not at load
# time) so adding a key later doesn't need a restart in dev.


@dataclass
class Provider:
    name: str
    base_url: str
    api_key_env: str
    models: dict            # {"explain": ..., "chat": ...}
    rpm_limit: int = 20

    # runtime state
    client: OpenAI | None = None
    circuit_open_until: float = 0.0     # monotonic time
    _rl_lock: threading.Lock = field(default_factory=threading.Lock)
    _next_allowed: float = 0.0

    def has_key(self) -> bool:
        return bool(os.environ.get(self.api_key_env))

    def get_client(self) -> OpenAI:
        if self.client is None:
            key = os.environ.get(self.api_key_env)
            if not key:
                raise RuntimeError(f"{self.api_key_env} is not set")
            self.client = OpenAI(base_url=self.base_url, api_key=key)
        return self.client

    def throttle(self) -> None:
        """Simple call spacing at this provider's own RPM budget."""
        min_interval = 60.0 / max(self.rpm_limit, 1)
        with self._rl_lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_allowed = max(now, self._next_allowed) + min_interval

    def circuit_open(self) -> bool:
        return time.monotonic() < self.circuit_open_until

    def trip_circuit(self, cooldown_seconds: float = 60.0) -> None:
        self.circuit_open_until = time.monotonic() + cooldown_seconds

    def model_for(self, tier: str) -> str:
        return self.models[tier]


class AllProvidersExhausted(RuntimeError):
    """Every configured provider failed or was rate-limited."""


def _load_providers() -> list[Provider]:
    cfg = _config_path()
    if not cfg.is_file():
        return []
    raw = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    out: list[Provider] = []
    for p in raw.get("providers", []):
        out.append(Provider(
            name=p["name"],
            base_url=p["base_url"],
            api_key_env=p.get("api_key_env", ""),
            models=p.get("models", {}),
            rpm_limit=int(p.get("rpm_limit", 20)),
        ))
    # legacy env-var overrides (v0–v3): CODEGRAPH_MODEL_EXPLAIN/CHAT remap the
    # NIM entry's tiers without touching the yaml
    for p in out:
        if p.name == "nim":
            if os.environ.get("CODEGRAPH_MODEL_EXPLAIN"):
                p.models["explain"] = os.environ["CODEGRAPH_MODEL_EXPLAIN"]
            if os.environ.get("CODEGRAPH_MODEL_CHAT"):
                p.models["chat"] = os.environ["CODEGRAPH_MODEL_CHAT"]
    return out


_providers: list[Provider] | None = None
_providers_lock = threading.Lock()


def get_providers() -> list[Provider]:
    global _providers
    if _providers is None:
        with _providers_lock:
            if _providers is None:
                _providers = _load_providers()
    return _providers


def reset_providers() -> None:
    """Reload config + clear circuit state (used by tests and by the /meta surface)."""
    global _providers
    with _providers_lock:
        _providers = _load_providers()


def _status() -> list[dict]:
    return [
        {
            "name": p.name,
            "has_key": p.has_key(),
            "circuit_open": p.circuit_open(),
            "rpm_limit": p.rpm_limit,
            "models": p.models,
        }
        for p in get_providers()
    ]


# ---------------------------------------------------------------------------
# Core completion with failover
# ---------------------------------------------------------------------------

def complete(system: str, user: str, tier: str = "chat", json_mode: bool = False,
             max_tokens: int = 1024, allow_partial: bool = False) -> tuple[str, str]:
    """Try each provider in config order; return (text, provider_name).

    - 429/503 from a provider trips its circuit for a cooldown and the next
      provider is tried immediately (per-provider breaker, not global backoff)
    - a truncated response raises like v3's _complete so callers can decide
    - no provider succeeds -> AllProvidersExhausted
    """
    errors: list[str] = []
    for provider in get_providers():
        if provider.circuit_open() or not provider.has_key():
            continue
        try:
            text = _complete_on(provider, system, user, tier, json_mode,
                                max_tokens, allow_partial)
            return text, provider.name
        except _RateLimited as e:
            provider.trip_circuit(float(os.environ.get("CODEGRAPH_CIRCUIT_COOLDOWN", "60")))
            errors.append(f"{provider.name}: rate limited ({e})")
        except _Truncated:
            raise
        except Exception as e:  # network error, bad model id, auth, ...
            errors.append(f"{provider.name}: {e}")
    raise AllProvidersExhausted(
        "no provider could serve the request — " + ("; ".join(errors) or "none configured with an API key")
    )


class _RateLimited(Exception):
    pass


class _Truncated(Exception):
    pass


def _complete_on(provider: Provider, system: str, user: str, tier: str,
                 json_mode: bool, max_tokens: int, allow_partial: bool) -> str:
    provider.throttle()
    client = provider.get_client()
    kwargs = {}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    resp = None
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=provider.model_for(tier),
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.2,
                max_tokens=max_tokens,
                **kwargs,
            )
            break
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None) or getattr(e, "status_code", None)
            if status == 429:
                raise _RateLimited(str(e)) from e
            transient = status in (503, 500) or "overloaded" in str(e).lower()
            if not transient or attempt == 2:
                raise
            time.sleep([5, 15][attempt])
    if resp is None:
        raise RuntimeError("request failed after retries")

    choice = resp.choices[0]
    if choice.finish_reason == "length":
        if allow_partial and choice.message.content:
            return choice.message.content
        raise _Truncated(
            "model output truncated at max_tokens — reduce injected context or raise CODEGRAPH_MAX_TOKENS"
        )
    return choice.message.content or ""


def complete_raw(system: str, user: str, tier: str = "chat",
                 max_tokens: int | None = None) -> tuple[str, str]:
    """Raw completion passthrough for the agent (diff generation).

    Same 2x token-retry-on-truncation strategy as v3, now across the whole
    provider chain: a truncated answer retries at 2x tokens before moving on.
    Returns (text, provider_name).
    """
    if max_tokens is None:
        max_tokens = int(os.environ.get("CODEGRAPH_MAX_TOKENS", "6144"))
    last_error: Exception | None = None
    for tokens in (max_tokens, max_tokens * 2):
        try:
            return complete(system=system + "\n/no_think", user=user, tier=tier,
                            json_mode=False, max_tokens=tokens)
        except _Truncated as e:
            last_error = e
    raise RuntimeError(str(last_error))


# ---------------------------------------------------------------------------
# Back-compat wrappers matching nim.py's signatures (callers unchanged)
# ---------------------------------------------------------------------------

def explain_node(node: dict, source: str, neighbors: list[dict], edges: list[str]) -> tuple[str, str]:
    """Explain one node grounded in its code + immediate graph neighborhood."""
    nb_lines = [f"- {n['id']} ({n['type']})" for n in neighbors]
    user = f"""Explain what this {node['type']} does, in plain English, for a developer
onboarding onto the codebase. Be concrete: what it takes in, what it returns, what it
depends on. If the docstring is present, trust it. Do NOT invent behavior not visible
in the code or neighborhood.

NODE: {node['id']} ({node['type']})
FILE: {node['file']} lines {node['line_start']}-{node['line_end']}
DOCSTRING: {node.get('docstring') or '(none)'}

GRAPH NEIGHBORS:
{chr(10).join(nb_lines) if nb_lines else '(none)'}

EDGES: {', '.join(edges) if edges else '(none)'}

SOURCE:
```python
{source}
```
"""
    return complete(
        system="You are a precise code explanation assistant. Answer only from the provided code and graph context.\n/no_think",
        user=user,
        tier="explain",
        max_tokens=int(os.environ.get("CODEGRAPH_EXPLAIN_TOKENS", "1536")),
        allow_partial=True,
    )


def chat_answer(question: str, node_blocks: str, relationships: str,
                known_nodes: list[dict] | None = None) -> tuple[dict, str]:
    """Answer grounded in the retrieved subgraph. Returns ({answer, cited_nodes}, provider)."""
    system = """You are CodeGraph, an assistant that answers questions about a codebase using a
dependency graph. You are given relevant nodes (with source) and their relationships.
Rules:
1. Answer ONLY from the provided nodes and relationships.
2. If the answer needs code not shown, say so explicitly.
3. End with a JSON object on its own line: {"cited_nodes": ["<node id>", ...]}
   listing the node ids you actually used."""
    user = f"""QUESTION: {question}

RELEVANT NODES:
{node_blocks}

RELATIONSHIPS:
{relationships if relationships else '(none among retrieved nodes)'}
"""
    raw, provider_name = complete(system=system, user=user, tier="chat", json_mode=False)

    # reasoning-tier models may emit <think>...</think> traces; strip them
    while "<think>" in raw and "</think>" in raw:
        start = raw.index("<think>")
        end = raw.index("</think>") + len("</think>")
        raw = (raw[:start] + raw[end:]).strip()

    cited: list[str] = []
    answer_text = raw
    json_matches = list(re.finditer(r"\{[^{}]*cited_nodes[^{}]*\}", raw, re.DOTALL))
    for m in reversed(json_matches):
        snippet = m.group(0).replace("'", '"')
        try:
            obj = json.loads(snippet)
            if isinstance(obj, dict) and isinstance(obj.get("cited_nodes"), list):
                cited = [c for c in obj["cited_nodes"] if isinstance(c, str)]
                answer_text = (raw[:m.start()] + raw[m.end():]).strip()
                break
        except json.JSONDecodeError:
            continue

    if not cited and known_nodes:
        candidates = sorted(
            (((n.get("qualname") or n["name"]), n["id"]) for n in known_nodes),
            key=lambda c: -len(c[0]),
        )
        text = raw
        for qual, nid in candidates:
            if qual and qual in text:
                cited.append(nid)
                text = text.replace(qual, "\u00b7" * len(qual))

    return {"answer": answer_text, "cited_nodes": cited}, provider_name


def last_provider_status() -> list[dict]:
    """Provider states for the /meta debug surface (user story #4/#5)."""
    return _status()
