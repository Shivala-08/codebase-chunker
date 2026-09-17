"""NVIDIA NIM client (OpenAI-compatible) + model tiering.

Uses https://integrate.api.nvidia.com/v1 per TRD §6. Two tiers:
- fast model for /node/{id}/explain (small context, fires often)
- strong model for /chat (carries expanded subgraphs)
Swapping models is a config change via env vars, not code.
"""
from __future__ import annotations

import json
import os
import threading
import time

from openai import OpenAI

BASE_URL = "https://integrate.api.nvidia.com/v1"

MODEL_EXPLAIN = os.environ.get("CODEGRAPH_MODEL_EXPLAIN", "nvidia/nemotron-3.5-lightning-30b-a3b")
MODEL_CHAT = os.environ.get("CODEGRAPH_MODEL_CHAT", "nvidia/nemotron-3-super-120b-a12b")


def _client() -> OpenAI:
    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key:
        raise RuntimeError(
            "NVIDIA_API_KEY is not set. Get one at https://build.nvidia.com "
            "and export NVIDIA_API_KEY=nvapi-..."
        )
    return OpenAI(base_url=BASE_URL, api_key=api_key)


# NIM enforces ~40 RPM per account (global, all models). Stay at 20 with
# simple call spacing so one buggy frontend loop can't spray the API.
_RPM_LIMIT = int(os.environ.get("CODEGRAPH_RPM_LIMIT", "20"))
_min_interval = 60.0 / _RPM_LIMIT
_rl_lock = threading.Lock()
_next_allowed = [0.0]


def _throttle():
    with _rl_lock:
        now = time.monotonic()
        wait = _next_allowed[0] - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _next_allowed[0] = max(now, _next_allowed[0]) + _min_interval


def _complete(system: str, user: str, model: str, json_mode: bool = False, max_tokens: int = 1024, allow_partial: bool = False) -> str:
    _throttle()
    client = _client()
    kwargs = {}
    if json_mode:
        # NIM honors OpenAI-style response_format on most chat models
        kwargs["response_format"] = {"type": "json_object"}
    # NIM intermittently returns 429/503 under load; a short backoff retry
    # turns those into latency instead of user-facing errors
    resp = None
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=model,
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
            transient = status in (429, 503) or "overloaded" in str(e).lower()
            if not transient or attempt == 2:
                raise
            time.sleep([5, 15][attempt])
    if resp is None:
        raise RuntimeError("NIM request failed after retries")
    choice = resp.choices[0]
    if choice.finish_reason == "length":
        # A truncated explanation is still worth showing; a truncated chat
        # answer would lose its cited_nodes JSON, so only explain may pass.
        if allow_partial and choice.message.content:
            return choice.message.content
        raise RuntimeError(
            "model output truncated at max_tokens — reduce injected context or raise CODEGRAPH_MAX_TOKENS"
        )
    return choice.message.content or ""


def complete_raw(system: str, user: str, model: str, max_tokens: int | None = None) -> str:
    """Raw completion passthrough for the agent (diff generation).

    Reasoning-tier models emit long thinking chains that count against the
    token budget — the 1024 default for /explain starves the actual diff,
    so this path defaults much higher (override with CODEGRAPH_MAX_TOKENS).
    The Nemotron '/no_think' toggle moves the chain into a separate
    reasoning_content field, but it still bills against max_tokens, and its
    length varies wildly between runs (observed 2k–21k chars for the same
    prompt) — so a fixed budget truncates nondeterministically. Retry once
    at 2× before giving up; two attempts bound the worst-case cost.
    """
    if max_tokens is None:
        max_tokens = int(os.environ.get("CODEGRAPH_MAX_TOKENS", "6144"))
    last_error: RuntimeError | None = None
    for tokens in (max_tokens, max_tokens * 2):
        try:
            return _complete(system=system + "\n/no_think", user=user, model=model,
                             json_mode=False, max_tokens=tokens)
        except RuntimeError as e:
            if "truncated" not in str(e):
                raise
            last_error = e
    raise last_error  # type: ignore[misc]


def explain_node(node: dict, source: str, neighbors: list[dict], edges: list[str]) -> str:
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
    # MODEL_EXPLAIN is a Nemotron reasoning-tier model: its thinking chain
    # counts against max_tokens, so /no_think is required or the budget dies
    # before the explanation starts (same fix as complete_raw).
    return _complete(
        system="You are a precise code explanation assistant. Answer only from the provided code and graph context.\n/no_think",
        user=user,
        model=MODEL_EXPLAIN,
        max_tokens=int(os.environ.get("CODEGRAPH_EXPLAIN_TOKENS", "1536")),
        allow_partial=True,
    )


def chat_answer(question: str, node_blocks: str, relationships: str, known_nodes: list[dict] | None = None) -> dict:
    """Answer grounded in the retrieved subgraph. Returns {answer, cited_nodes}."""
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
    raw = _complete(system=system, user=user, model=MODEL_CHAT, json_mode=False)

    # reasoning-tier models may emit <think>...</think> traces; strip them
    while "<think>" in raw and "</think>" in raw:
        start = raw.index("<think>")
        end = raw.index("</think>") + len("</think>")
        raw = (raw[:start] + raw[end:]).strip()

    # Extract cited node ids. Models emit the trailing JSON in varying shapes
    # (fenced, mid-answer, single quotes), so try structured parsing first and
    # fall back to matching known node ids as literal substrings of the answer.
    import re

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
        # models cite by qualname ("Graph.add_edges_from"), not full ids —
        # longest-first so "DiGraph.x" can't double-count as "Graph.x"
        candidates = sorted(
            (((n.get("qualname") or n["name"]), n["id"]) for n in known_nodes),
            key=lambda c: -len(c[0]),
        )
        text = raw
        for qual, nid in candidates:
            if qual and qual in text:
                cited.append(nid)
                text = text.replace(qual, "\u00b7" * len(qual))

    return {"answer": answer_text, "cited_nodes": cited}
