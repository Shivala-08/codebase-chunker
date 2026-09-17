# CodeGraph

AI tool that parses a Python codebase into a dependency graph, visualizes it, and answers questions grounded in that graph — instead of raw-text RAG or blind grepping.

## Stack

- **Parser**: tree-sitter (`tree-sitter-python`) → nodes (modules/classes/functions/methods) + edges (`imports`, `imports_symbol`, `calls`, `contains`) → `graph.json`
- **Graph store**: `networkx.DiGraph`, in-memory, rebuilt per run
- **Backend**: FastAPI — `POST /parse`, `GET /graph`, `GET /node/{id}/explain`, `POST /chat`, `POST /agent/task`, `POST /docs/generate`, `GET /docs[/{page}]`
- **AI**: NVIDIA NIM (`https://integrate.api.nvidia.com/v1`, OpenAI-compatible). Small model for `/explain`, strong model for `/chat`, `/agent/task` and doc narratives — swap via env vars.
- **Frontend**: React + Vite + `react-force-graph-2d`

## Setup

```bash
# 1. Backend
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # paste your NVIDIA_API_KEY (get one at https://build.nvidia.com)

# 2. Frontend (new terminal)
cd frontend
npm install
```

## Run

```bash
# terminal 1 — backend
cd backend && source .venv/bin/activate
uvicorn main:app --reload --port 8000

# terminal 2 — frontend
cd frontend && npm run dev
```

Open http://localhost:5173, type the **absolute path** of a Python repo (try `sample-repo/shop` from the project root — absolute path required by the parser), hit **Parse**, then:

- click any node → LLM explanation grounded in its code + neighbors
- ask "how does login talk to the database?" → answer cites nodes, cited nodes highlight in the graph
- type a task in the **Agent task** box → proposed diff on a new branch (see below)

## Agent tasks (v1, Mode 3)

The agent reuses the chat retrieval pipeline, generates a unified diff, and hands you a reviewable branch. Guardrails, enforced in code (`backend/codegraph/agent.py`):

- **Never auto-merges.** It proposes; you approve.
- **Your checkout is never touched** — the diff is applied and committed in a throwaway `git worktree` on a new `codegraph/<task>` branch.
- Refuses to run on a dirty worktree (the diff must match HEAD, not your uncommitted edits).
- The model's diff is sanitized and validated (`git apply --check`) — it is data, never executed.
- If the repo has tests, they run sandboxed in a subprocess with a hard timeout, inside the worktree, and the result is shown next to the diff — never silently trusted.
- Opens a PR when a remote allows it (`gh` CLI, then `GITHUB_TOKEN` API fallback); otherwise leaves the commit on the branch for local review.

```bash
curl -X POST localhost:8000/agent/task -H 'Content-Type: application/json' \
  -d '{"repo_path": "/abs/path/sample-repo/shop", "task": "add a docstring to save_order"}'
```

## Living docs (v2, Mode 4)

Hit **📚 Generate docs** (or `POST /docs/generate`) to build a markdown doc site from the current graph:

- one page per directory cluster: an LLM purpose narrative grounded in docstrings + a **Mermaid diagram transformed mechanically from the graph edges** (the LLM never draws)
- an index page with a cross-cluster module map
- pages served at `GET /docs/{page}`, browsable in the Docs panel

Regeneration is manual for v2; a git pre-push/CI hook is the natural v3 upgrade. If NIM is unreachable, pages fall back to docstring-derived narratives so generation never blocks.

```bash
curl -X POST localhost:8000/docs/generate
curl localhost:8000/docs           # list pages
curl localhost:8000/docs/cluster.md
```

> FastAPI's built-in Swagger console lives at `/api-docs` (moved off `/docs`).

## CLI (no server)

```bash
cd backend
python -m codegraph.parser ../sample-repo/shop out.json
```

## Known limitations (v0/v1, by design)

- Static call resolution is best-effort (~80%): dynamic dispatch, `getattr`, decorators missed
- Keyword-match retrieval (no embeddings yet)
- Graph rebuilt per run; no persistence
- Python only for now (JS/TS next)
- Agent diffs must apply cleanly against HEAD; ambiguous tasks fall back to `NEEDS_CONTEXT` rather than guessing
- Hunk line numbers from the model are re-anchored against file reality before apply; unmatchable hunks are rejected
- Docs cluster by directory — repos with a single directory get one page; narratives depend on docstring quality
