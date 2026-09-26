"""CodeGraph CLI (v4): use the tool from a terminal without the frontend.

Thin wrapper that imports the backend functions directly (no HTTP hop).
All LLM-backed commands go through the provider chain, so failover works
here exactly as in the server.

Usage:
  python -m codegraph.cli parse <repo> [-o graph.json]
  python -m codegraph.cli chat "<question>" [--graph graph.json]
  python -m codegraph.cli explain <node-id> [--graph graph.json]
  python -m codegraph.cli blast-radius <node-id> [--graph graph.json] [--hops N]
  python -m codegraph.cli hotspots [--graph graph.json] [--limit N]
  python -m codegraph.cli export [--graph graph.json] [--format json|graphml|dot]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .graph_store import GraphStore
from .parser import parse_repo, write_graph


def _default_graph_path() -> Path:
    return Path(__file__).parent.parent / "data" / "graph.json"


def _load_store(args) -> GraphStore:
    path = Path(args.graph) if args.graph else _default_graph_path()
    if not path.is_file():
        sys.exit(f"no graph at {path} — run `codegraph parse <repo>` first")
    return GraphStore(path)


def cmd_parse(args) -> int:
    t0 = __import__("time").time()
    graph = parse_repo(args.repo)
    out = write_graph(graph, args.out)
    print(f"parsed {graph['repo']}: {len(graph['nodes'])} nodes, "
          f"{len(graph['edges'])} edges -> {out} "
          f"({__import__('time').time() - t0:.1f}s)")
    return 0


def cmd_chat(args) -> int:
    from . import nim  # provider chain via the shim

    store = _load_store(args)
    node_ids = store.get_subgraph_for_query(args.question, max_nodes=18, expand_hops=2)
    if not node_ids:
        print("no graph nodes matched that question", file=sys.stderr)
        return 1

    blocks = []
    for nid in node_ids:
        n = store.get_node(nid)
        if n is None:
            continue
        blocks.append(f"- {nid} ({n['type']}) — neighbors: "
                      f"{', '.join(store.get_neighbors(nid, depth=1)[:6]) or '(none)'}\n"
                      f"  {(n.get('docstring') or '')[:200]}")
    relationships = "\n".join(store.describe_edges(node_ids))

    try:
        result, provider = nim.chat_answer(
            args.question, "\n".join(blocks), relationships,
            known_nodes=[store.get_node(nid) for nid in node_ids if store.get_node(nid)],
        )
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    valid = set(store.node_data.keys())
    cited = [c for c in result["cited_nodes"] if c in valid]
    if args.show_provider:
        print(f"[provider: {provider}]")
    print(result["answer"].strip())
    if cited:
        print("\nCited nodes:")
        for c in cited:
            print(f"  - {c}")
    return 0


def cmd_explain(args) -> int:
    from . import nim

    store = _load_store(args)
    node = store.get_node(args.node_id)
    if node is None:
        print(f"unknown node: {args.node_id}", file=sys.stderr)
        return 1
    neighbor_ids = store.get_neighbors(args.node_id, depth=1)
    neighbor_nodes = [store.get_node(n) for n in neighbor_ids if store.get_node(n)]
    edges = []
    for nb in neighbor_ids:
        for t in store.get_edge_types(args.node_id, nb):
            edges.append(f"{args.node_id} -[{t}]-> {nb}")

    repo = store.repo
    p = Path(repo) / node["file"]
    source = "(source unavailable)"
    if p.exists():
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        source = "\n".join(lines[node["line_start"] - 1:node["line_end"]])

    try:
        answer, provider = nim.explain_node(node, source, neighbor_nodes, edges)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.show_provider:
        print(f"[provider: {provider}]")
    print(answer.strip())
    return 0


def cmd_blast_radius(args) -> int:
    from .graph_analysis import blast_radius

    store = _load_store(args)
    if store.get_node(args.node_id) is None:
        print(f"unknown node: {args.node_id}", file=sys.stderr)
        return 1
    result = blast_radius(store, args.node_id, max_hops=args.hops)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    print(f"{result['total']} node(s) affected within {args.hops} hop(s):")
    for ring in result["rings"]:
        for entry in ring["nodes"]:
            print(f"  [hop {ring['hop']}] {entry['id']} ({entry['type']}) "
                  f"— {entry['file']}:{entry['line_start']}")
    if result["total"] == 0:
        print("  (nothing depends on this node)")
    return 0


def cmd_hotspots(args) -> int:
    from .graph_analysis import hotspots

    store = _load_store(args)
    spots = hotspots(store, limit=args.limit)
    if args.json:
        print(json.dumps(spots, indent=2))
        return 0
    print(f"top {len(spots)} hotspots (fan-in + fan-out):")
    for s in spots:
        print(f"  {s['degree']:>4}  {s['id']}  (in {s['fan_in']} / out {s['fan_out']}) — {s['file']}")
    return 0


def cmd_export(args) -> int:
    from .graph_io import export

    store = _load_store(args)
    fmt = args.format
    if fmt == "json":
        print(Path(args.graph or _default_graph_path()).read_text(encoding="utf-8"))
        return 0
    print(export(store, fmt))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="codegraph",
                                 description="CodeGraph — dependency-graph tool for codebases")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("parse", help="parse a repo into graph.json")
    p.add_argument("repo")
    p.add_argument("-o", "--out", default=str(_default_graph_path()))
    p.set_defaults(func=cmd_parse)

    p = sub.add_parser("chat", help="ask a question about the parsed repo")
    p.add_argument("question")
    p.add_argument("--graph", help="path to graph.json (default: backend/data/graph.json)")
    p.add_argument("--show-provider", action="store_true", help="print which provider served the request")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("explain", help="explain one node (LLM)")
    p.add_argument("node_id")
    p.add_argument("--graph")
    p.add_argument("--show-provider", action="store_true")
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("blast-radius", help="what breaks if I change this node (no LLM)")
    p.add_argument("node_id")
    p.add_argument("--hops", type=int, default=3)
    p.add_argument("--graph")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_blast_radius)

    p = sub.add_parser("hotspots", help="most-connected nodes (no LLM)")
    p.add_argument("--limit", type=int, default=15)
    p.add_argument("--graph")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_hotspots)

    p = sub.add_parser("export", help="export the graph (json|graphml|dot)")
    p.add_argument("--graph")
    p.add_argument("--format", default="json", choices=["json", "graphml", "dot"])
    p.set_defaults(func=cmd_export)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
