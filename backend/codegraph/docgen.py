"""Living docs (v2, Mode 4): graph -> clustered markdown pages + Mermaid.

Walks the graph, clusters nodes by directory (the §2.2 graph structure is the
only data model — no new storage), and produces one markdown page per cluster:

  - a Mermaid flowchart rendered *mechanically* from the cluster's edges
    (a direct graph -> Mermaid syntax transform; the LLM never draws)
  - an LLM-written purpose / entry-points narrative, grounded in node
    docstrings and signatures — never invented behavior

Index page links all clusters + cross-cluster import edges. Regeneration is
a manual `POST /docs/generate` for v2; a git hook is the v3 upgrade.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from . import nim


def _slug(text: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text or "cluster"


_TEST_FILE_RE = re.compile(r"(^|/)(test_[^/]*|[^/]*_test)\.py$|(^|/)tests?/")


def _is_test_node(store, node_id: str) -> bool:
    n = store.node_data.get(node_id)
    return bool(n and _TEST_FILE_RE.search(n["file"]))


def _short_id(node_id: str) -> str:
    """Mermaid-safe short id, deterministic per node."""
    h = hashlib.md5(node_id.encode()).hexdigest()[:6]
    return f"n{h}"


def _esc_label(text: str) -> str:
    """Escape a label for Mermaid double-quoted strings."""
    return text.replace("\\", "\\\\").replace('"', "#quot;").replace("\n", " ")


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------

def cluster_by_directory(store) -> dict[str, list[str]]:
    """Group symbol nodes by their containing directory (repo-root files -> '(root)').

    Module-level nodes are skipped: their `contains` children carry the content.
    """
    clusters: dict[str, list[str]] = {}
    for nid, n in store.node_data.items():
        if n["type"] == "module":
            continue
        d = str(Path(n["file"]).parent)
        clusters.setdefault(d, []).append(nid)
    return dict(sorted(clusters.items()))


# ---------------------------------------------------------------------------
# Mermaid transform (mechanical — no LLM involved)
# ---------------------------------------------------------------------------

_EDGES_WORTH_SHOWING = ("calls", "imports_symbol")


def mermaid_for_cluster(store, node_ids: list[str], max_nodes: int = 30) -> str:
    """Direct graph -> Mermaid flowchart for one cluster's internal edges."""
    keep = set(node_ids)
    if len(keep) > max_nodes:
        # keep the most central nodes so the diagram stays readable
        ranked = sorted(
            keep,
            key=lambda nid: -store._centrality.get(nid, 0.0),
        )
        keep = set(ranked[:max_nodes])

    id_map = {nid: _short_id(nid) for nid in keep}
    lines = ["flowchart LR"]
    for nid in sorted(keep):
        n = store.node_data[nid]
        label = _esc_label(n.get("qualname") or n["name"])
        lines.append(f'    {id_map[nid]}["{label}"]')

    seen: set[tuple[str, str, str]] = set()
    for u, v, d in store.g.edges(data=True):
        if u in keep and v in keep and u != v:
            etype = d.get("type", "related")
            if etype not in _EDGES_WORTH_SHOWING:
                continue
            key = (u, v, etype)
            if key in seen:
                continue
            seen.add(key)
            arrow = "-->" if etype == "calls" else "-.->"
            lines.append(f"    {id_map[u]} {arrow}|{etype}| {id_map[v]}")
    return "\n".join(lines)


def mermaid_cross_cluster(store, clusters: dict[str, list[str]]) -> str:
    """Import edges between clusters, one node per directory."""
    dir_of = {}
    for d, ids in clusters.items():
        for nid in ids:
            dir_of[nid] = d

    dshort = {d: _short_id(f"dir:{d}") for d in clusters}
    lines = ["flowchart LR"]
    for d in sorted(clusters):
        label = _esc_label(d)
        lines.append(f'    {dshort[d]}["{label}"]')

    seen: set[tuple[str, str]] = set()
    for u, v, d in store.g.edges(data=True):
        du, dv = dir_of.get(u), dir_of.get(v)
        if du and dv and du != dv and d.get("type") in ("imports", "imports_symbol", "calls"):
            key = (du, dv)
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"    {dshort[du]} -->|imports| {dshort[dv]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM page narrative (purpose + entry points, docstring-grounded)
# ---------------------------------------------------------------------------

def _page_narrative(cluster_name: str, nodes: list[dict], relationships: str, test_note: str = "") -> str:
    sym_lines = []
    for n in nodes[:40]:
        doc = (n.get("docstring") or "").strip().replace("\n", " ")[:160]
        sig = f"{n.get('qualname') or n['name']} ({n['type']})"
        sym_lines.append(f"- {sig} — {doc}" if doc else f"- {sig}")
    system = """You write onboarding documentation for developers. You are given the symbols of
one module/directory of a codebase, their docstrings, and their relationships.
Rules:
1. Describe ONLY what the docstrings/signatures/relationships imply. Never invent behavior.
2. If the purpose is unclear from what's shown, say what is unclear.
3. Be concrete and brief: 2-4 sentences of purpose, then 2-4 key entry points with one-line reasons.4. Format: a '**Purpose**' paragraph, then a '**Key entry points**' bullet list. No headers, no code blocks."""
    if test_note:
        system += f"\n\nNote: {test_note} Summarize the test suite in one sentence at the end."
    user = f"""MODULE/DIRECTORY: {cluster_name}

SYMBOLS:
{chr(10).join(sym_lines) if sym_lines else "(none)"}

RELATIONSHIPS (internal):
{relationships if relationships else "(none)"}

Write the narrative now."""
    # a narrative is a few sentences; the cap keeps NIM latency bounded
    try:
        return nim.complete_raw(system=system, user=user, model=nim.MODEL_CHAT)
    except (RuntimeError, Exception) as e:
        # NIM unreachable / truncation / rate limit -> docstring-derived fallback,
        # so a flaky LLM can never block doc generation
        return _fallback_narrative(cluster_name, nodes, test_note, error=str(e))


def _fallback_narrative(cluster_name: str, nodes: list[dict], test_note: str, error: str) -> str:
    """Mechanical narrative from docstrings when the LLM path fails."""
    doced = [n for n in nodes if (n.get("docstring") or "").strip()]
    parts = [f"**Purpose** — {cluster_name} contains {len(nodes)} symbols "
             f"({sum(1 for n in nodes if n['type'] == 'class')} classes, "
             f"{sum(1 for n in nodes if n['type'] in ('function', 'method'))} functions/methods).",
             ""]
    if doced:
        parts.append("**Key entry points**")
        parts.append("")
        for n in doced[:6]:
            doc = (n.get("docstring") or "").strip().splitlines()[0]
            parts.append(f"- `{n.get('qualname') or n['name']}`: {doc}")
    else:
        parts.append("_(no docstrings present — see the symbol list below)_")
    if test_note:
        parts += ["", test_note]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Page assembly
# ---------------------------------------------------------------------------

def _page(store, cluster_name: str, node_ids: list[str]) -> str:
    # source symbols drive the narrative/diagram; test symbols get a compact
    # summary so 24 test functions don't bury 19 source symbols
    src_ids = [nid for nid in node_ids if not _is_test_node(store, nid)]
    test_ids = [nid for nid in node_ids if _is_test_node(store, nid)]

    by_type: dict[str, list[dict]] = {}
    for nid in src_ids:
        n = store.node_data[nid]
        by_type.setdefault(n["type"], []).append(n)

    test_files = sorted({store.node_data[nid]["file"] for nid in test_ids})
    narrative = _page_narrative(
        cluster_name,
        [store.node_data[nid] for nid in src_ids],
        "\n".join(store.describe_edges(src_ids)),
        test_note=(f"This module also has a test suite: {', '.join(test_files)} "
                   f"({len(test_ids)} test symbols)." if test_ids else ""),
    )
    narrative = narrative.strip()
    # models may wrap output in fences or think-traces even when told not to
    while "<think>" in narrative and "</think>" in narrative:
        s = narrative.index("<think>")
        e = narrative.index("</think>") + len("</think>")
        narrative = (narrative[:s] + narrative[e:]).strip()

    parts = [f"# {cluster_name}", "", narrative, ""]
    parts += ["## Structure", "", "```mermaid", mermaid_for_cluster(store, src_ids), "```", ""]

    t0 = store.repo + "/"
    for t in ("class", "function", "method"):
        if not by_type.get(t):
            continue
        parts.append(f"### {t}s")
        parts.append("")
        for n in sorted(by_type[t], key=lambda x: x["id"]):
            loc = n["file"].removeprefix(t0) + f":{n['line_start']}"
            doc = (n.get("docstring") or "").strip().splitlines()[0] if n.get("docstring") else ""
            parts.append(f"- `{n.get('qualname') or n['name']}` — *{loc}*" + (f": {doc}" if doc else ""))
        parts.append("")

    cross = set()
    keep = set(node_ids)
    for u, v, d in store.g.edges(data=True):
        if d.get("type") != "imports":
            continue
        if u in keep and v not in keep and store.node_data.get(v, {}).get("type") == "module":
            cross.add(f"- imports `{v.removeprefix(t0)}`")
        if v in keep and u not in keep and store.node_data.get(u, {}).get("type") == "module":
            cross.add(f"- imported by `{u.removeprefix(t0)}`")
    if cross:
        parts += ["## Cross-module imports", "", *sorted(cross), ""]
    if test_files:
        parts += ["## Tests", "", f"Test suite: {', '.join(f'`{t.removeprefix(t0)}`' for t in test_files)} "
                  f"({len(test_ids)} test symbols).", ""]
    return "\n".join(parts)


def generate_docs(store, out_dir: str | Path) -> dict:
    """Generate the doc site. Returns stats for the API response."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    clusters = cluster_by_directory(store)
    pages: dict[str, str] = {}

    index_links = []
    for d, ids in clusters.items():
        slug = _slug(d)
        (out / f"{slug}.md").write_text(_page(store, d, ids), encoding="utf-8")
        pages[slug] = d
        index_links.append((d, slug, len(ids)))

    index = ["# Codebase docs", "", f"_Auto-generated from the dependency graph of `{store.repo}`._", ""]
    if len(clusters) > 1:
        index += ["## Module map", "", "```mermaid", mermaid_cross_cluster(store, clusters), "```", ""]
    index += ["## Modules", ""]
    for d, slug, n in sorted(index_links):
        index.append(f"- [{d}]({slug}.md) ({n} symbols)")
    index.append("")
    (out / "index.md").write_text("\n".join(index), encoding="utf-8")

    return {"out_dir": str(out), "clusters": len(clusters), "pages": len(pages) + 1}
