"""Graph analysis (v4): blast radius + hotspots.

Pure graph math on the GraphStore — no LLM, no I/O beyond the store itself.
These are the fast, deterministic answers you want before touching code:
what breaks if I change this (blast radius), and what's risky to touch at
all (hotspots).
"""
from __future__ import annotations

import networkx as nx


def blast_radius(store, node_id: str, max_hops: int = 3) -> dict:
    """Transitive reverse reachability from `node_id`, up to `max_hops`.

    Walks INCOMING edges only (imports/imports_symbol/calls all point from
    consumer -> provider, so reverse = dependents). Returns per-hop rings so
    the UI can show "direct callers" vs "transitive".
    """
    if node_id not in store.g:
        return {"root": node_id, "hops": max_hops, "total": 0, "rings": [], "affected": []}

    # reverse graph: edge u->v becomes v->u, so successors = dependents
    reverse = store.g.reverse(copy=False)
    rings: list[dict] = []
    seen: set[str] = {node_id}
    frontier = {node_id}
    for hop in range(1, max_hops + 1):
        nxt: set[str] = set()
        for nid in frontier:
            for dep in reverse.successors(nid):
                if dep not in seen:
                    seen.add(dep)
                    nxt.add(dep)
        if not nxt:
            break
        rings.append({
            "hop": hop,
            "nodes": [
                _affected_entry(store, nid, node_id, hop)
                for nid in sorted(nxt)
            ],
        })
        frontier = nxt

    affected = [nid for nid in sorted(seen) if nid != node_id]
    return {
        "root": node_id,
        "hops": max_hops,
        "total": len(affected),
        "by_type": _count_by_type(store, affected),
        "rings": rings,
        "affected": affected,
    }


def _affected_entry(store, nid: str, root: str, hop: int) -> dict:
    n = store.node_data.get(nid, {})
    return {
        "id": nid,
        "type": n.get("type", "unknown"),
        "file": n.get("file", ""),
        "line_start": n.get("line_start"),
        "hop": hop,
    }


def _count_by_type(store, node_ids: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for nid in node_ids:
        t = store.node_data.get(nid, {}).get("type", "unknown")
        counts[t] = counts.get(t, 0) + 1
    return counts


def hotspots(store, limit: int = 15) -> list[dict]:
    """Rank nodes by fan-in + fan-out (in/out degree), plus centrality.

    Module nodes are excluded: a module with many `contains` edges is not a
    risk hotspot in the same sense a heavily-called function is.
    """
    scores: list[tuple[float, int, int, str]] = []
    for nid in store.g.nodes:
        n = store.node_data.get(nid)
        if not n or n["type"] == "module":
            continue
        indeg = store.g.in_degree(nid)
        outdeg = store.g.out_degree(nid)
        total = indeg + outdeg
        if total == 0:
            continue
        scores.append((float(total), indeg, outdeg, nid))

    scores.sort(key=lambda t: (-t[0], -t[1], t[3]))
    out: list[dict] = []
    for total, indeg, outdeg, nid in scores[:limit]:
        n = store.node_data[nid]
        out.append({
            "id": nid,
            "type": n["type"],
            "file": n["file"],
            "degree": total,
            "fan_in": indeg,
            "fan_out": outdeg,
            "centrality": round(store._centrality.get(nid, 0.0), 4),
        })
    return out
