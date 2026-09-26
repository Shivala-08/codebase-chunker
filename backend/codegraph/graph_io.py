"""Graph I/O (v4): export + snapshot diff.

Export uses networkx's built-in writers (GraphML, DOT) — the JSON form is
the graph.json we already produce. The snapshot diff is pure set comparison
over two saved graph.json files: what nodes/edges appeared, disappeared, or
changed between commits.
"""
from __future__ import annotations

import json
from pathlib import Path

import networkx as nx


def export(store, fmt: str) -> str:
    """Serialize the store's graph as graphml or dot."""
    if fmt == "graphml":
        # GraphML needs string node attrs; copy with everything stringified
        g = nx.DiGraph()
        for nid, n in store.node_data.items():
            g.add_node(nid, **{k: str(v) for k, v in n.items() if v is not None})
        for u, v, d in store.g.edges(data=True):
            g.add_edge(u, v, **{k: str(val) for k, val in d.items()})
        import io
        buf = io.BytesIO()   # networkx writes GraphML as bytes
        nx.write_graphml(g, buf)
        return buf.getvalue().decode("utf-8")
    if fmt == "dot":
        # native writer — pygraphviz is a system-graphviz-dependent install
        # we don't want to force on users
        return _to_dot(store)
    raise ValueError(f"unsupported export format: {fmt}")


def _dot_quote(text: str) -> str:
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ') + '"'


def _to_dot(store) -> str:
    lines = ["digraph codegraph {", "  rankdir=LR;"]
    for nid in sorted(store.node_data):
        n = store.node_data[nid]
        lines.append(f"  {_dot_quote(nid)} [label={_dot_quote(n.get('qualname') or nid)}, type={_dot_quote(n['type'])}];")
    for u, v, d in sorted(store.g.edges(data=True)):
        lines.append(f"  {_dot_quote(u)} -> {_dot_quote(v)} [label={_dot_quote(d.get('type', 'related'))}];")
    lines.append("}")
    return "\n".join(lines)


def load_snapshot(path: str | Path) -> dict:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"snapshot not found: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def diff_snapshots(a: dict, b: dict) -> dict:
    """Set-diff two graph.json snapshots (A=before, B=after).

    A node "changed" when its (type, file, line_start, line_end) tuple moved —
    e.g. a function that grew or moved within its file. Edge diff keys on
    (from, to, type).
    """
    def node_key(n: dict) -> tuple:
        return (n["id"], n["type"], n["file"], n.get("line_start"), n.get("line_end"))

    a_nodes = {n["id"]: n for n in a.get("nodes", [])}
    b_nodes = {n["id"]: n for n in b.get("nodes", [])}
    a_ids, b_ids = set(a_nodes), set(b_nodes)

    a_edges = {(e["from"], e["to"], e["type"]) for e in a.get("edges", [])}
    b_edges = {(e["from"], e["to"], e["type"]) for e in b.get("edges", [])}

    added_nodes = sorted(b_ids - a_ids)
    removed_nodes = sorted(a_ids - b_ids)
    changed_nodes = []
    for nid in sorted(a_ids & b_ids):
        if node_key(a_nodes[nid]) != node_key(b_nodes[nid]):
            changed_nodes.append({
                "id": nid,
                "before": {k: a_nodes[nid].get(k) for k in ("type", "file", "line_start", "line_end")},
                "after": {k: b_nodes[nid].get(k) for k in ("type", "file", "line_start", "line_end")},
            })

    # edges only count when both endpoints exist in the respective snapshot
    a_valid = {e for e in a_edges if e[0] in a_ids and e[1] in a_ids}
    b_valid = {e for e in b_edges if e[0] in b_ids and e[1] in b_ids}

    return {
        "nodes": {
            "added": added_nodes,
            "removed": removed_nodes,
            "changed": changed_nodes,
        },
        "edges": {
            "added": sorted(f"{u} -[{t}]-> {v}" for u, v, t in b_valid - a_valid),
            "removed": sorted(f"{u} -[{t}]-> {v}" for u, v, t in a_valid - b_valid),
        },
        "summary": {
            "nodes_before": len(a_ids),
            "nodes_after": len(b_ids),
            "nodes_added": len(added_nodes),
            "nodes_removed": len(removed_nodes),
            "nodes_changed": len(changed_nodes),
            "edges_added": len(b_valid - a_valid),
            "edges_removed": len(a_valid - b_valid),
        },
    }
