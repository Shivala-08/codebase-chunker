# CodeGraph

AI tool that parses a codebase into a dependency graph, visualizes it, and answers questions grounded in that graph — instead of raw-text RAG or blind grepping.

## Stack

- **Parser**: tree-sitter with a language registry (`backend/codegraph/parser.py`) → nodes (modules/classes/functions/methods) + edges (`imports`, `imports_symbol`, `calls`, `contains`) → `graph.json`. One repo = one graph, even with mixed languages (v4).
- **Graph store**: `networkx.DiGraph`, in-memory, rebuilt per run
- **Backend**: FastAPI — `POST /parse`, `GET /graph`, `GET /node/{id}/explain`, `POST /chat`, `POST /agent/task`, `POST /docs/generate`, `GET /docs[/{page}]`, plus the v4 endpoints below. Also serves the built frontend, so the whole app runs off one server.
- **AI**: provider chain (v4) — NVIDIA NIM primary, OpenRouter/Gemini/Groq as automatic failover (`backend/codegraph/providers.yaml`), with per-provider rate limiting and circuit breakers. Only the keys you set are used.
- **Frontend**: React + Vite + `react-force-graph-2d`. Built once with `npm run build` into `frontend/dist`; the backend serves it at `http://localhost:8000`.

## Languages (v4)

| Language | Extensions | Notes |
|---|---|---|
| Python | `.py` | full symbol + import + call resolution |
| JS/TS | `.js .jsx .mjs .cjs .ts .tsx` | functions/classes/methods, import/require edges |
| Java | `.java` | classes/interfaces/methods/ctors, import + call edges |
| Go | `.go` | functions + receiver methods, struct/interface types |
| C# | `.cs` | classes/structs/interfaces/methods (P1) |
| C / C++ | `.c .h .cpp .cc .cxx .hpp .hh` | functions, structs/classes, `#include` edges (best-effort) |
| Rust | `.rs` | functions, structs/enums/traits, `use` edges (best-effort) |

Unknown extensions are skipped, not errored. Adding a language = one registry entry + one ~80-line extractor in `parser.py` — no parser rewrite (PRD v4 story #2). Cross-language call edges (JS calling a Python endpoint) are **not** resolved automatically; both languages still land in the same graph. A grammar whose wheel didn't install is skipped per-file; you can also force-disable grammars with `CODEGRAPH_DISABLED_LANGUAGES="c-sharp,rust"`.

## Provider fallback (v4)

`/explain`, `/chat`, agent tasks and doc narratives all go through the chain in `backend/codegraph/providers.yaml`: NIM → OpenRouter → Gemini → Groq, tried in order.

- **429 from one provider trips its circuit for 60s** and the next provider is tried immediately in the same request — no global backoff, no stalled sessions (PRD story #3)
- each provider keeps its **own** RPM budget (`rpm_limit` in the yaml)
- the response includes `"provider": "<name>"` (and `via <name>` in the chat UI) so you can see who actually served it (story #4)
- reorder or disable providers by editing the yaml — no code changes (story #5); a provider with no key in `.env` is skipped silently

## Insights: blast radius + hotspots (v4)

Pure graph math — no LLM, instant:

```bash
curl 'localhost:8000/node/db.py::save_order/blast-radius?hops=3'   # what breaks if I change this?
curl 'localhost:8000/graph/hotspots?limit=15'                      # most-connected, riskiest nodes
```

Blast radius walks incoming edges transitively (per-hop rings: direct callers first). Hotspots rank non-module nodes by fan-in + fan-out. Both are in the 📐 **Insights** panel; hotspot nodes also render larger in the graph.

## Auto re-parse on save (v4)

After any `/parse`, a `watchdog` file watcher follows the parsed repo. Saving a file with a known extension triggers a debounced full re-parse (~1.5s) — no manual re-parse, no restart. `POST /watch/stop` / `POST /watch/start` control it; `/meta` shows its state.

## Export + snapshot diff (v4)

```bash
curl 'localhost:8000/graph/export?format=graphml' -o graph.graphml   # or dot, or json

curl -X POST localhost:8000/graph/diff -H 'Content-Type: application/json' \
  -d '{"graph_a": "old/graph.json", "graph_b": "new/graph.json"}'   # added/removed/changed
```

Export feeds Gephi/yEd/any DOT viewer. The diff is a pure set comparison over two `graph.json` snapshots — pair it with two commits to see what a PR structurally touched. Download buttons live in Insights → ⚙️ Status.

## CLI (no server, v4)

```bash
cd backend
.venv/bin/python -m codegraph.cli parse ../sample-repo/shop
.venv/bin/python -m codegraph.cli chat "how does login verify a password?" --show-provider
.venv/bin/python -m codegraph.cli blast-radius db.py::save_order --hops 2
.venv/bin/python -m codegraph.cli hotspots --limit 10
.venv/bin/python -m codegraph.cli export --format dot
```

`chat`/`explain` use the same provider chain; `blast-radius`/`hotspots`/`export` need no API key at all.

## Setup

```bash
# 1. Backend
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # paste your NVIDIA_API_KEY (get one at https://build.nvidia.com)

# 2. Frontend
cd frontend
npm install
npm run build     # produces frontend/dist, served by the backend
```

## Run (single server)

```bash
cd backend && source .venv/bin/activate
uvicorn main:app --reload --port 8000
```

Open **http://localhost:8000** — that's it. The backend serves both the API and the built frontend; no Vite, no second terminal.

The last parsed graph is persisted in `backend/data/graph.json` and auto-loads on page refresh, so you usually don't need to re-parse. Type the **absolute path** of a Python repo (try `sample-repo/shop` from the project root — absolute path required by the parser), hit **Parse**, then:

- click any node → LLM explanation grounded in its code + neighbors
- ask "how does login talk to the database?" → answer cites nodes, cited nodes highlight in the graph
- type a task in the **Agent task** box → proposed diff on a new branch (see below)

> **Frontend dev mode (optional):** `cd frontend && npm run dev` starts Vite with hot reload at http://localhost:5173 (it proxies `/api` to :8000, so the backend must be running). After editing frontend code without Vite, run `npm run build` and refresh :8000.

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

## Parser CLI (quick dump)

```bash
cd backend
python -m codegraph.parser ../sample-repo/shop out.json
```

## Known limitations (by design)

- Static call resolution is best-effort (~80%): dynamic dispatch, `getattr`, decorators missed
- Keyword-match retrieval (no embeddings yet)
- Graph rebuilt per run; no persistence
- Cross-language call edges (JS calling a Python endpoint) are not resolved — both graphs coexist but stay separate
- C/C++ and Rust extractors are deliberately best-effort; header/module resolution is a real problem left unsolved for v4
- File-watch re-parse is whole-repo, debounced — not incremental per-file patching (the TRD's sanctioned first pass)
- Agent diffs must apply cleanly against HEAD; ambiguous tasks fall back to `NEEDS_CONTEXT` rather than guessing
- Hunk line numbers from the model are re-anchored against file reality before apply; unmatchable hunks are rejected
- Docs cluster by directory — repos with a single directory get one page; narratives depend on docstring quality
