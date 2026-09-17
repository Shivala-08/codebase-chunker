"""In-memory graph store over networkx.DiGraph (v0: rebuild per run).

Loads graph.json and exposes the query functions the chat/explain layers need:
neighbors, keyword-seeded subgraph retrieval with structural expansion.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import networkx as nx

STOPWORDS = {
    "how", "does", "do", "the", "a", "an", "what", "is", "are", "in", "on",
    "of", "to", "and", "or", "if", "i", "change", "changed", "work", "works",
    "this", "that", "with", "for", "from", "when", "where", "which", "who",
    "can", "get", "got", "it", "its", "be", "by", "my", "me", "use", "used",
    "using", "code", "repo", "codebase", "project", "function", "functions",
}


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text.lower())
    return [w for w in words if w not in STOPWORDS and len(w) > 2]


def _subtokens(text: str) -> set[str]:
    """Split snake_case / camelCase names into parts for fuzzy matching."""
    parts: set[str] = set()
    for w in _tokens(text):
        parts.add(w)
        for p in re.split(r"[_\s]", w):
            if len(p) > 2:
                parts.add(p)
        for p in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", w):
            if len(p) > 2:
                parts.add(p.lower())
    return parts


class GraphStore:
    def __init__(self, graph_json_path: str | Path):
        self.path = Path(graph_json_path)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.repo: str = raw.get("repo", "")
        self.node_data: dict[str, dict] = {}
        self.g = nx.DiGraph()
        for n in raw["nodes"]:
            self.node_data[n["id"]] = n
            self.g.add_node(n["id"], **n)
        for e in raw["edges"]:
            if e["from"] in self.node_data and e["to"] in self.node_data:
                self.g.add_edge(e["from"], e["to"], type=e["type"])
        self._centrality: dict[str, float] = {}
        try:
            self._centrality = nx.degree_centrality(self.g)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Basic queries
    # ------------------------------------------------------------------

    def get_node(self, node_id: str) -> dict | None:
        return self.node_data.get(node_id)

    def get_neighbors(self, node_id: str, depth: int = 1) -> list[str]:
        """All nodes within `depth` hops, callers and callees alike."""
        if node_id not in self.g:
            return []
        found = nx.single_source_shortest_path_length(self.g.to_undirected(), node_id, cutoff=depth)
        found.pop(node_id, None)
        return list(found)

    def get_edge_types(self, src: str, dst: str) -> list[str]:
        types: list[str] = []
        if self.g.has_edge(src, dst):
            types.append(self.g.edges[src, dst].get("type", "related"))
        if self.g.has_edge(dst, src):
            types.append(self.g.edges[dst, src].get("type", "related"))
        return types

    # ------------------------------------------------------------------
    # Retrieval (v0: keyword scoring, then structural expansion)
    # ------------------------------------------------------------------

    def _seed_scores(self, question: str) -> dict[str, float]:
        q_tokens = _subtokens(question)
        scores: dict[str, float] = {}
        for node_id, n in self.node_data.items():
            if n["type"] == "module":
                continue
            hay = _subtokens(f"{n['name']} {n.get('qualname','')} {n['file']} {n.get('docstring') or ''}")
            if not hay:
                continue
            overlap = q_tokens & hay
            if not overlap:
                continue
            # exact name hits outweigh docstring hits
            name_parts = _subtokens(n["name"])
            score = 0.0
            for t in overlap:
                score += 3.0 if t in name_parts else 1.0
            score *= 1.0 + 0.15 * self._centrality.get(node_id, 0.0)
            if "test" in n["file"].lower():
                score *= 0.3  # tests match question wording often; demote them
            scores[node_id] = score
        return scores

    def get_subgraph_for_query(self, question: str, max_nodes: int = 18,
                               expand_hops: int = 2) -> list[str]:
        """Top-k seed nodes by keyword match, expanded by 1-2 graph hops.

        This is the structural retrieval that differentiates CodeGraph from
        text-chunk RAG: relevance follows call/import edges, not similarity.
        """
        seeds = self._seed_scores(question)
        if not seeds:
            return []
        ranked = sorted(seeds.items(), key=lambda kv: kv[1], reverse=True)
        top_seeds = [nid for nid, _ in ranked[: max(3, max_nodes // 3)]]

        combined: dict[str, float] = {}
        for nid, s in seeds.items():
            combined[nid] = s
        depth_weights = {1: 0.6, 2: 0.3}
        for seed in top_seeds:
            for depth in range(1, expand_hops + 1):
                for nb in self.get_neighbors(seed, depth=depth):
                    if nb in self.node_data and self.node_data[nb]["type"] == "module":
                        continue
                    combined[nb] = max(combined.get(nb, 0.0), depth_weights.get(depth, 0.1))

        final = sorted(combined.items(), key=lambda kv: kv[1], reverse=True)
        return [nid for nid, _ in final[:max_nodes]]

    def describe_edges(self, node_ids: list[str]) -> list[str]:
        """Human-readable relationship lines between the selected nodes."""
        keep = set(node_ids)
        lines: list[str] = []
        for u, v, d in self.g.edges(data=True):
            if u in keep and v in keep:
                lines.append(f"{u} -> {v} ({d.get('type', 'related')})")
        return lines
