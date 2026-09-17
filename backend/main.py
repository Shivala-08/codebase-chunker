"""FastAPI backend for CodeGraph.

Endpoints per TRD §2.3: POST /parse, GET /graph,
GET /node/{id}/explain, POST /chat.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from codegraph.graph_store import GraphStore
from codegraph import nim
from codegraph.parser import parse_repo, write_graph
from codegraph.agent import run_task, AgentError
from codegraph import docgen

load_dotenv()  # picks up backend/.env if present

# NOTE: FastAPI's built-in Swagger UI defaults to /docs — that would shadow the
# living-docs endpoints below, so the console moves to /api-docs.
app = FastAPI(title="CodeGraph", version="0.1.0", docs_url="/api-docs", redoc_url="/api-redoc")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # local tool; tighten if you ever host it
    allow_methods=["*"],
    allow_headers=["*"],
)

GRAPH_PATH = Path(os.environ.get("CODEGRAPH_GRAPH_JSON", Path(__file__).parent / "data" / "graph.json"))

_state: dict = {"store": None, "repo": None, "built_at": None, "explain_cache": {}}


def _store() -> GraphStore:
    if _state["store"] is None:
        if not GRAPH_PATH.exists():
            raise HTTPException(status_code=404, detail="No graph built yet. POST /parse first.")
        _state["store"] = GraphStore(GRAPH_PATH)
    return _state["store"]


class ParseRequest(BaseModel):
    repo_path: str


class ChatRequest(BaseModel):
    question: str


class AgentTaskRequest(BaseModel):
    repo_path: str
    task: str


DOCS_DIR = Path(os.environ.get("CODEGRAPH_DOCS_DIR", Path(__file__).parent / "data" / "docs"))


@app.post("/parse")
def parse(req: ParseRequest):
    t0 = time.time()
    try:
        graph = parse_repo(req.repo_path)
    except NotADirectoryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    out = write_graph(graph, GRAPH_PATH)
    _state["store"] = GraphStore(out)
    _state["repo"] = graph["repo"]
    _state["built_at"] = time.time()
    return {
        "repo": graph["repo"],
        "nodes": len(graph["nodes"]),
        "edges": len(graph["edges"]),
        "graph_json": str(out),
        "seconds": round(time.time() - t0, 2),
    }


@app.get("/graph")
def graph():
    if not GRAPH_PATH.exists():
        raise HTTPException(status_code=404, detail="No graph built yet. POST /parse first.")
    # NOTE: return the file's text as the response body, NOT as a Python str
    # return value — FastAPI JSON-encodes str returns into a JSON *string*,
    # which double-encodes the payload and breaks frontend hydration.
    # no-store: caching this large payload makes Chrome's CORS-restart path
    # deadlock on an incomplete cache entry and the body never resolves.
    return Response(
        content=GRAPH_PATH.read_text(encoding="utf-8"),
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/meta")
def meta():
    return {"repo": _state["repo"], "built_at": _state["built_at"], "graph_json": str(GRAPH_PATH)}


def _read_source(file: str, line_start: int, line_end: int, repo_root: str | None) -> str:
    if repo_root:
        p = Path(repo_root) / file
        if p.exists():
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(lines[line_start - 1:line_end])
    return "(source unavailable)"


@app.get("/node/{node_id:path}/explain")
def explain(node_id: str):
    store = _store()
    node = store.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"unknown node: {node_id}")

    neighbor_ids = store.get_neighbors(node_id, depth=1)
    neighbor_nodes = [store.get_node(n) for n in neighbor_ids]
    neighbor_nodes = [n for n in neighbor_nodes if n]

    edges = []
    for nb in neighbor_ids:
        for t in store.get_edge_types(node_id, nb):
            edges.append(f"{node_id} -[{t}]-> {nb}")

    # cache explanations per node (per graph build) — NIM is rate-limited, so
    # clicking the same node twice must not cost a second API call
    cache_key = (store.path.stat().st_mtime, node_id)
    cached = _state["explain_cache"].get(cache_key)
    if cached is not None:
        return {"node_id": node_id, "explanation": cached, "neighbors": neighbor_ids, "cached": True}

    source = _read_source(node["file"], node["line_start"], node["line_end"], store.repo)
    try:
        answer = nim.explain_node(node, source, neighbor_nodes, edges)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    _state["explain_cache"][cache_key] = answer
    return {"node_id": node_id, "explanation": answer, "neighbors": neighbor_ids, "cached": False}


@app.post("/chat")
def chat(req: ChatRequest):
    store = _store()
    node_ids = store.get_subgraph_for_query(req.question, max_nodes=18, expand_hops=2)
    if not node_ids:
        raise HTTPException(status_code=404, detail="No graph nodes matched that question.")

    blocks = []
    for nid in node_ids:
        n = store.get_node(nid)
        if n is None:
            continue
        src = _read_source(n["file"], n["line_start"], n["line_end"], store.repo)
        # cap each snippet so 18 nodes stay within context
        src_lines = src.splitlines()
        if len(src_lines) > 25:
            src = "\n".join(src_lines[:25]) + f"\n... (+{len(src_lines) - 25} lines)"
        rel = store.get_neighbors(nid, depth=1)
        blocks.append(
            f"- {nid} ({n['type']}) — neighbors: {', '.join(rel[:6]) or '(none)'}\n"
            f"  {(n.get('docstring') or '')[:200]}\n"
            f"  ```python\n  {src}\n  ```"
        )
    node_blocks = "\n".join(blocks)
    relationships = "\n".join(store.describe_edges(node_ids))

    try:
        result = nim.chat_answer(
            req.question, node_blocks, relationships,
            known_nodes=[store.get_node(nid) for nid in node_ids if store.get_node(nid)],
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    valid = set(store.node_data.keys())
    cited = [c for c in result["cited_nodes"] if c in valid]
    return {
        "answer": result["answer"],
        "used_nodes": node_ids,     # what retrieval injected
        "cited_nodes": cited,       # what the model said it used
    }


@app.post("/agent/task")
def agent_task(req: AgentTaskRequest):
    """Mode 3 (v1): graph-grounded task -> proposed diff on a NEW branch.

    Guardrails (TRD §8): never auto-merges; refuses a dirty worktree; runs the
    repo's tests sandboxed (subprocess + timeout); opens a PR when a remote
    allows it, otherwise leaves the commit for local review.
    """
    if not req.task.strip():
        raise HTTPException(status_code=400, detail="task must not be empty")
    try:
        result = run_task(req.repo_path, req.task.strip(), _store())
    except AgentError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return result.to_dict()


@app.post("/docs/generate")
def docs_generate():
    """Mode 4 (v2): regenerate the living-docs site from the current graph.

    One page per directory cluster: LLM purpose narrative (docstring-grounded)
    + a Mermaid diagram transformed mechanically from the graph edges.
    """
    store = _store()
    try:
        stats = docgen.generate_docs(store, DOCS_DIR)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return stats


@app.get("/docs")
def docs_list():
    pages = sorted(p.name for p in DOCS_DIR.glob("*.md")) if DOCS_DIR.is_dir() else []
    if not pages:
        raise HTTPException(status_code=404, detail="No docs generated yet. POST /docs/generate first.")
    return {"pages": pages, "out_dir": str(DOCS_DIR)}


@app.get("/docs/{page}")
def docs_page(page: str):
    if not re.fullmatch(r"[a-z0-9-]+\.md", page):
        raise HTTPException(status_code=400, detail="invalid page name")
    path = DOCS_DIR / page
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"unknown doc page: {page}")
    return Response(content=path.read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8")


# --- serve the built frontend (frontend/dist) so the app runs off a single
# server: http://localhost:8000 — no vite needed. `npm run build` refreshes it.
FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"
if (FRONTEND_DIST / "index.html").is_file():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(FRONTEND_DIST / "index.html")
